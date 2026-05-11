"""
인도네시아 법령 인덱싱 - 연속 실행 루프

- 1회만 돌리려면: python ingest_loop.py --once
- 5시간 동안 반복: python ingest_loop.py --duration 5h
- 매 사이클: 폴더 스캔 → 변경/신규 파일만 처리 → 삭제된 파일의 청크 제거
- 병렬: PDF 파싱은 ProcessPoolExecutor, 임베딩은 메인 프로세스 배치
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time

# Windows 기본 콘솔(cp949)에서 ↻ ✓ 등 unicode 출력 시 UnicodeEncodeError로 cycle이
# 죽는 것을 방지. 워커처럼 stdout/stderr를 UTF-8로 강제.
for _stream_name in ("stdout", "stderr"):
    _s = getattr(sys, _stream_name, None)
    if _s is not None and hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path

from index_manager import (
    CHROMA_DIR, MANIFEST_PATH, SOURCE_ROOT,
    FileEntry, Manifest,
    discover_pdfs, hash_file, make_chunk_id, normalize_category,
    parse_pdf_worker,
    MAX_PDF_BYTES,
)

from sentence_transformers import SentenceTransformer

# rag_chroma 헬퍼: HttpClient(기본) / PersistentClient 모드 분기.
from rag_chroma import CHROMA_MODE, describe_target, get_chroma_client

EMBEDDING_MODEL = os.getenv(
    "RAG_EMBEDDING_MODEL",
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
)
EMBED_BATCH = int(os.getenv("RAG_EMBED_BATCH", "128"))
# 워커 프로세스 수 — 각 워커가 모델을 따로 들고 있으므로 메모리 ~500MB/워커.
# CPU 18코어, 메모리 16GB → 6워커가 적당 (워커 ~3GB + 메인 + 시스템).
PARSE_WORKERS = int(os.getenv("RAG_PARSE_WORKERS", "6"))
# 사이클당 한 번에 처리할 최대 파일 수 (중간 저장 단위)
BATCH_SIZE = int(os.getenv("RAG_BATCH_SIZE", "200"))
# 한 PDF 파싱+임베딩 timeout (초)
PARSE_TIMEOUT = int(os.getenv("RAG_PARSE_TIMEOUT", "120"))
# upsert를 누적해서 보낼 청크 임계치
UPSERT_FLUSH_CHUNKS = int(os.getenv("RAG_UPSERT_FLUSH_CHUNKS", "2048"))
# ChromaDB upsert 한 번 호출당 최대 청크 (ChromaDB 내부 제한 5461 미만으로)
UPSERT_BATCH_LIMIT = int(os.getenv("RAG_UPSERT_BATCH_LIMIT", "5000"))

# 메인 프로세스는 임베딩하지 않으므로 BLAS 스레드 적게
os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("MKL_NUM_THREADS", "2")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "2")


def parse_duration(s: str) -> timedelta:
    """e.g. '5h', '30m', '120s', '1h30m'"""
    s = s.strip().lower()
    total = 0
    for n, unit in re.findall(r"(\d+)\s*([hms])", s):
        n = int(n)
        if unit == "h":
            total += n * 3600
        elif unit == "m":
            total += n * 60
        elif unit == "s":
            total += n
    if total == 0:
        # 숫자만 들어오면 초로 해석
        try:
            total = int(s)
        except ValueError:
            raise argparse.ArgumentTypeError(f"잘못된 duration: {s}")
    return timedelta(seconds=total)


def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def get_collection(client, name: str):
    """존재하면 가져오고, 없으면 생성."""
    try:
        return client.get_collection(name)
    except Exception:
        return client.create_collection(name=name, metadata={"hnsw:space": "cosine"})


def scan_filesystem() -> dict[str, tuple[Path, str, str, int, float]]:
    """현재 디스크에 있는 PDF들을 (path → (Path, category, sha256, size, mtime))로 반환.

    sha256은 만약 파일이 manifest에 이미 있고 size+mtime이 같으면 계산 생략(성능 최적화)
    호출자에서 manifest와 비교하여 hash 필요 여부 결정.
    """
    out = {}
    for category, pdf_path in discover_pdfs():
        try:
            stat = pdf_path.stat()
        except OSError:
            continue
        out[str(pdf_path)] = (pdf_path, category, stat.st_size, stat.st_mtime)
    return out


def detect_changes(manifest: Manifest):
    """현재 디스크 상태와 manifest 비교 → (added, modified, deleted) 리스트.

    빠른 판정: size+mtime 같으면 unchanged 가정. 다르면 sha256 검사.
    """
    current = scan_filesystem()
    manifest_paths = manifest.all_paths()
    deleted = sorted(manifest_paths - set(current.keys()))

    pending: list[tuple[Path, str, str, int, float]] = []  # (path, category, sha, size, mtime)
    retried = 0
    skipped_oversize = 0
    for path_str, (pdf_path, category, size, mtime) in current.items():
        e = manifest.entries.get(path_str)
        # chunk_count==0은 이전 사이클에서 파싱/임베딩이 실패한 엔트리.
        # size+mtime이 같아도 재시도해야 한다 (워커 OOM·BrokenProcessPool 등 일과성 실패 회수).
        prev_failed = bool(e and e.chunk_count == 0)
        # 단, MAX_PDF_BYTES 초과 oversize 파일은 워커에서 항상 fail이므로 영구 스킵 —
        # 매 사이클마다 워커 슬롯·시간을 낭비하지 않도록.
        if prev_failed and size > MAX_PDF_BYTES:
            skipped_oversize += 1
            continue
        if e and e.size == size and abs(e.mtime - mtime) < 1.0 and not prev_failed:
            continue  # unchanged & 정상 인덱싱됨
        # 의심되면 hash 계산
        try:
            sha, size2, mtime2 = hash_file(pdf_path)
        except OSError as exc:
            log(f"  ! hash 실패 {pdf_path.name}: {exc}")
            continue
        if e and e.sha256 == sha and not prev_failed:
            # 내용 동일 (touch만 됨) — manifest mtime만 갱신
            e.mtime = mtime2
            manifest.upsert(e)
            continue
        if prev_failed:
            retried += 1
        pending.append((pdf_path, category, sha, size2, mtime2))
    if retried:
        log(f"  ↻ 이전 실패 엔트리 재시도 대상: {retried}개")
    if skipped_oversize:
        log(f"  ⊘ oversize 영구 스킵: {skipped_oversize}개 (>{MAX_PDF_BYTES//1024//1024}MB)")
    return pending, deleted


def remove_deleted(manifest: Manifest, client, deleted_paths: list[str]) -> int:
    """manifest에는 있지만 디스크엔 없는 파일들의 청크를 ChromaDB에서 제거."""
    removed_chunks = 0
    by_collection: dict[str, list[str]] = {}
    for p in deleted_paths:
        e = manifest.entries.get(p)
        if not e or not e.chunk_ids:
            manifest.delete(p)
            continue
        by_collection.setdefault(e.collection, []).extend(e.chunk_ids)
        manifest.delete(p)
        removed_chunks += len(e.chunk_ids)
    for col_name, ids in by_collection.items():
        try:
            col = get_collection(client, col_name)
            # ChromaDB delete는 5000개 단위로 분할
            for i in range(0, len(ids), 5000):
                col.delete(ids=ids[i:i+5000])
        except Exception as exc:
            log(f"  ! 컬렉션 {col_name} 삭제 실패: {exc}")
    return removed_chunks


def index_files(
    manifest: Manifest, client, model,  # model 인자는 사용 안 함 (워커가 자체 보유). 호환성 유지.
    pending: list[tuple[Path, str, str, int, float]],
    progress_every: int = 50,
) -> tuple[int, int, int]:
    """pending 파일들을 ProcessPool로 병렬 파싱+임베딩, 메인은 ChromaDB upsert만.

    Returns: (files_processed, chunks_added, errors)
    """
    if not pending:
        return 0, 0, 0

    meta_by_path: dict[str, tuple[str, str, int, float]] = {
        str(p): (cat, sha, size, mtime) for p, cat, sha, size, mtime in pending
    }

    files_done = 0
    chunks_total = 0
    errors = 0

    col_cache: dict[str, object] = {}

    def get_col(name: str):
        if name not in col_cache:
            col_cache[name] = get_collection(client, name)
        return col_cache[name]

    # 워커에 (path, category) 튜플로 전달 (워커가 컬렉션명 직접 계산)
    work_items = [(str(p), cat) for p, cat, _, _, _ in pending]
    total = len(work_items)

    # 배치를 따로 만들지 않고 한 풀에 모두 submit (manifest 저장은 N파일마다)
    log(f"  ProcessPool: workers={PARSE_WORKERS}, embed_batch={EMBED_BATCH}, total={total}")

    def make_error_entry(path_str: str) -> FileEntry:
        cat, sha, size, mtime = meta_by_path[path_str]
        return FileEntry(
            path=path_str, category=cat,
            collection=normalize_category(cat),
            sha256=sha, size=size, mtime=mtime,
            chunk_ids=[], chunk_count=0,
            indexed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    # 워커가 임베딩까지 완료해서 보내므로 메인은 upsert만 함.
    # 메모리 절약 위해 일정 청크 누적 시 즉시 upsert.
    buffer_files: list[dict] = []
    buffer_chunks_count = 0

    def flush_buffer() -> None:
        nonlocal buffer_chunks_count, chunks_total, errors
        if not buffer_files:
            return
        # 컬렉션별 그룹핑
        groups: dict[str, dict] = {}
        for f in buffer_files:
            g = groups.setdefault(f["collection"], {
                "ids": [], "texts": [], "metas": [], "embs": [],
            })
            g["ids"].extend(f["ids"])
            g["texts"].extend(f["texts"])
            g["metas"].extend(f["metas"])
            g["embs"].extend(f["embeddings"])

        success_collections = set()
        for col_name, g in groups.items():
            n = len(g["ids"])
            try:
                col = get_col(col_name)
                # ChromaDB max_batch_size(=5461) 초과 방지: 5000 단위로 분할 upsert.
                # 동일 id에 대한 upsert는 idempotent하므로 부분 성공 후 재시도 안전.
                for i in range(0, n, UPSERT_BATCH_LIMIT):
                    j = min(i + UPSERT_BATCH_LIMIT, n)
                    col.upsert(
                        ids=g["ids"][i:j], documents=g["texts"][i:j],
                        metadatas=g["metas"][i:j], embeddings=g["embs"][i:j],
                    )
                success_collections.add(col_name)
            except Exception as exc:
                errors += 1
                log(f"  ! upsert 실패 [{col_name}] ({n}청크): {type(exc).__name__}: {exc}")

        for f in buffer_files:
            if f["collection"] in success_collections:
                cat = f["category"]
                sha = f["sha"]; size = f["size"]; mtime = f["mtime"]
                manifest.upsert(FileEntry(
                    path=f["path"], category=cat, collection=f["collection"],
                    sha256=sha, size=size, mtime=mtime,
                    chunk_ids=f["ids"], chunk_count=len(f["ids"]),
                    indexed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ))
                chunks_total += len(f["ids"])
            else:
                manifest.upsert(make_error_entry(f["path"]))

        buffer_files.clear()
        buffer_chunks_count = 0

    # ProcessPool로 모든 파일을 한 번에 submit. 워커 init_worker가 모델 미리 로드.
    from embed_worker import parse_and_embed, init_worker

    t_start = time.time()
    last_progress_time = t_start

    with ProcessPoolExecutor(max_workers=PARSE_WORKERS, initializer=init_worker) as pool:
        futures = {pool.submit(parse_and_embed, item): item[0] for item in work_items}
        log(f"  ✓ {len(futures)}개 작업 submit 완료, 결과 수신 시작")
        for fut in as_completed(futures):
            path_str = futures[fut]
            try:
                result = fut.result(timeout=PARSE_TIMEOUT)
            except Exception as exc:
                errors += 1
                log(f"  ! 워커 예외 {Path(path_str).name}: {type(exc).__name__}")
                manifest.upsert(make_error_entry(path_str))
                files_done += 1
                continue

            if "error" in result:
                errors += 1
                log(f"  ! {Path(path_str).name}: {result['error'][:100]}")
                manifest.upsert(make_error_entry(path_str))
                files_done += 1
                continue

            ids = result.get("ids") or []
            if not ids:
                manifest.upsert(make_error_entry(path_str))
                files_done += 1
                continue

            cat, sha, size, mtime = meta_by_path[path_str]

            # 기존 청크 삭제
            old_entry = manifest.entries.get(path_str)
            if old_entry and old_entry.chunk_ids:
                try:
                    old_col = get_col(old_entry.collection)
                    for i in range(0, len(old_entry.chunk_ids), 5000):
                        old_col.delete(ids=old_entry.chunk_ids[i:i+5000])
                except Exception:
                    pass

            buffer_files.append({
                "path": path_str,
                "collection": result["collection"],
                "category": cat,
                "sha": sha, "size": size, "mtime": mtime,
                "ids": ids,
                "texts": result["texts"],
                "metas": result["metas"],
                "embeddings": result["embeddings"],
            })
            buffer_chunks_count += len(ids)
            files_done += 1

            if buffer_chunks_count >= UPSERT_FLUSH_CHUNKS:
                flush_buffer()
                manifest.save()

            if files_done % progress_every == 0:
                now = time.time()
                rate = progress_every / max(0.1, now - last_progress_time)
                last_progress_time = now
                log(
                    f"  진행 {files_done}/{total} "
                    f"(chunks {chunks_total}+{buffer_chunks_count} buf, "
                    f"errors {errors}, {rate:.1f}f/s = {rate*60:.0f}f/min)"
                )

    flush_buffer()
    manifest.save()
    elapsed = time.time() - t_start
    log(f"  ✓ 전체 완료: {files_done}/{total} files, {chunks_total} chunks, {errors} err, {elapsed:.0f}s")

    return files_done, chunks_total, errors


def cycle(manifest: Manifest, client, model: SentenceTransformer) -> dict:
    t0 = time.time()
    pending, deleted = detect_changes(manifest)
    removed = remove_deleted(manifest, client, deleted) if deleted else 0
    if deleted:
        log(f"  삭제 감지: 파일 {len(deleted)}개 / 청크 {removed}개 제거")
    # 처리 순서: (a) 신규 미인덱스(manifest에 없음) 우선 → (b) 그 다음 size 작은 것 먼저.
    # 신규 PDF가 사용자가 가장 빨리 보고 싶어할 결과이고, 작은 파일은 워커 한 슬롯을
    # 짧게 점유해서 큰 파일이 끼어 있어도 throughput을 안정화한다.
    manifest_paths = manifest.all_paths()
    pending.sort(key=lambda x: (str(x[0]) in manifest_paths, x[3]))
    total_files = total_chunks = total_errs = 0
    if pending:
        log(f"  처리 대상: 파일 {len(pending)}개 (workers={PARSE_WORKERS}, batch={BATCH_SIZE})")
        # 큰 pending 리스트는 BATCH_SIZE 단위로 잘라서 처리 — 매 배치마다 ProcessPool을
        # 새로 띄우므로 워커 메모리(모델/캐시)가 해제된다. 16GB 시스템에서 OOM 방지.
        for i in range(0, len(pending), BATCH_SIZE):
            batch = pending[i:i + BATCH_SIZE]
            log(f"  ── batch {i//BATCH_SIZE + 1}/{(len(pending)+BATCH_SIZE-1)//BATCH_SIZE} ({len(batch)} files) ──")
            f, c, e = index_files(manifest, client, model, batch)
            total_files += f; total_chunks += c; total_errs += e
            manifest.save()
            # 약간 쉬어 OS가 메모리 회수 시간 확보
            time.sleep(1)
    return {
        "files_processed": total_files, "chunks_added": total_chunks, "errors": total_errs,
        "deleted_files": len(deleted), "removed_chunks": removed,
        "elapsed_sec": time.time() - t0,
    }


def collection_stats(client) -> dict[str, int]:
    out = {}
    for col in client.list_collections():
        try:
            out[col.name] = col.count()
        except Exception:
            out[col.name] = -1
    return out


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="1회 실행 후 종료")
    parser.add_argument(
        "--duration", type=parse_duration, default=parse_duration("5h"),
        help="총 실행 시간 (예: 5h, 30m, 1h30m). 기본 5h",
    )
    parser.add_argument(
        "--interval", type=parse_duration, default=parse_duration("60s"),
        help="사이클 간 대기 시간. 기본 60s",
    )
    args = parser.parse_args(argv)

    log(f"SOURCE_ROOT = {SOURCE_ROOT}")
    log(f"CHROMA_DIR  = {CHROMA_DIR}")
    log(f"MANIFEST    = {MANIFEST_PATH}")

    if not SOURCE_ROOT.exists():
        log(f"[오류] SOURCE_ROOT 없음: {SOURCE_ROOT}")
        return 1

    # 메인 프로세스는 임베딩하지 않음 — 워커가 각자 모델을 로드한다.
    # 호환을 위해 None을 model 자리에 넘긴다 (index_files는 사용하지 않음).
    model = None
    log(f"ChromaDB 클라이언트 초기화... mode={CHROMA_MODE} target={describe_target()}")
    # persistent 모드일 때만 디렉터리 생성. http 모드는 chroma run 서버가 관리.
    if CHROMA_MODE == "persistent":
        CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    client = get_chroma_client()
    manifest = Manifest()
    log(f"manifest entries 로드: {len(manifest.entries)}개")

    started = time.time()
    deadline = started + args.duration.total_seconds() if not args.once else started + 1
    cycle_idx = 0

    while True:
        cycle_idx += 1
        log(f"--- cycle #{cycle_idx} ---")
        try:
            stats = cycle(manifest, client, model)
            log(
                f"  결과: 파일 {stats['files_processed']}건 처리 / "
                f"청크 {stats['chunks_added']}개 추가 / "
                f"에러 {stats['errors']}건 / "
                f"삭제 {stats['deleted_files']}건 / "
                f"{stats['elapsed_sec']:.1f}초"
            )
        except KeyboardInterrupt:
            log("중단됨 (Ctrl+C)")
            break
        except Exception as exc:
            log(f"  ! 사이클 예외: {type(exc).__name__}: {exc}")

        # 컬렉션 통계 (매 5사이클마다)
        if cycle_idx % 5 == 0 or args.once:
            try:
                stats_by_col = collection_stats(client)
                total = sum(v for v in stats_by_col.values() if v >= 0)
                log(f"  컬렉션 합계: {total}청크 ({len(stats_by_col)}컬렉션)")
                for name, cnt in sorted(stats_by_col.items()):
                    log(f"    - {name}: {cnt}")
            except Exception:
                pass

        if args.once:
            break

        now = time.time()
        remaining = deadline - now
        if remaining <= 0:
            log(f"⏰ 종료 시각 도달 (총 {(now - started)/3600:.2f}h 가동)")
            break
        wait = min(args.interval.total_seconds(), max(1.0, remaining))
        log(f"  다음 사이클까지 {wait:.0f}초 대기 (남은 시간 {remaining/60:.1f}분)")
        time.sleep(wait)

    manifest.save()
    log("manifest 저장 후 정상 종료")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
