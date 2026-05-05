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
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path

from index_manager import (
    CHROMA_DIR, MANIFEST_PATH, SOURCE_ROOT,
    FileEntry, Manifest,
    discover_pdfs, hash_file, make_chunk_id, normalize_category,
    parse_pdf_worker,
)

import chromadb
from chromadb.config import Settings
from sentence_transformers import SentenceTransformer

EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"
EMBED_BATCH = 64
PARSE_WORKERS = max(1, (os.cpu_count() or 4) - 1)


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
    for path_str, (pdf_path, category, size, mtime) in current.items():
        e = manifest.entries.get(path_str)
        if e and e.size == size and abs(e.mtime - mtime) < 1.0:
            continue  # unchanged
        # 의심되면 hash 계산
        try:
            sha, size2, mtime2 = hash_file(pdf_path)
        except OSError as exc:
            log(f"  ! hash 실패 {pdf_path.name}: {exc}")
            continue
        if e and e.sha256 == sha:
            # 내용 동일 (touch만 됨) — manifest mtime만 갱신
            e.mtime = mtime2
            manifest.upsert(e)
            continue
        pending.append((pdf_path, category, sha, size2, mtime2))
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
    manifest: Manifest, client, model: SentenceTransformer,
    pending: list[tuple[Path, str, str, int, float]],
    progress_every: int = 25,
) -> tuple[int, int, int]:
    """pending 파일들을 병렬 파싱 + 임베딩 + ChromaDB upsert.

    Returns: (files_processed, chunks_added, errors)
    """
    if not pending:
        return 0, 0, 0

    # 경로 → (category, sha, size, mtime) 매핑 보관
    meta_by_path: dict[str, tuple[str, str, int, float]] = {
        str(p): (cat, sha, size, mtime) for p, cat, sha, size, mtime in pending
    }

    files_done = 0
    chunks_total = 0
    errors = 0

    # 컬렉션 캐시 (생성 비용 절약)
    col_cache: dict[str, object] = {}

    def get_col(name: str):
        if name not in col_cache:
            col_cache[name] = get_collection(client, name)
        return col_cache[name]

    pdf_paths = [str(p) for p, _, _, _, _ in pending]
    total = len(pdf_paths)

    with ProcessPoolExecutor(max_workers=PARSE_WORKERS) as pool:
        futures = {pool.submit(parse_pdf_worker, p): p for p in pdf_paths}
        for fut in as_completed(futures):
            path_str = futures[fut]
            try:
                result = fut.result()
            except Exception as exc:
                errors += 1
                log(f"  ! 워커 예외 {Path(path_str).name}: {exc}")
                continue

            chunks = result.get("chunks") or []
            # 워커가 에러 dict를 반환한 경우
            if chunks and isinstance(chunks[0], dict) and chunks[0].get("_error"):
                errors += 1
                log(f"  ! 파싱 실패 {Path(path_str).name}: {chunks[0]['_error']}")
                # 빈 entry로 manifest에 기록 (다음 사이클에서 재시도 안 하도록)
                cat, sha, size, mtime = meta_by_path[path_str]
                manifest.upsert(FileEntry(
                    path=path_str, category=cat,
                    collection=normalize_category(cat),
                    sha256=sha, size=size, mtime=mtime,
                    chunk_ids=[], chunk_count=0,
                    indexed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ))
                files_done += 1
                continue

            if not chunks:
                cat, sha, size, mtime = meta_by_path[path_str]
                manifest.upsert(FileEntry(
                    path=path_str, category=cat,
                    collection=normalize_category(cat),
                    sha256=sha, size=size, mtime=mtime,
                    chunk_ids=[], chunk_count=0,
                    indexed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                ))
                files_done += 1
                continue

            cat, sha, size, mtime = meta_by_path[path_str]
            collection_name = normalize_category(cat)
            collection = get_col(collection_name)
            pdf_path_obj = Path(path_str)

            # 기존 청크가 있다면 삭제 (변경된 파일 처리)
            old_entry = manifest.entries.get(path_str)
            if old_entry and old_entry.chunk_ids:
                try:
                    old_col = get_col(old_entry.collection)
                    for i in range(0, len(old_entry.chunk_ids), 5000):
                        old_col.delete(ids=old_entry.chunk_ids[i:i+5000])
                except Exception:
                    pass  # 무시

            # 청크 ID 생성 + 텍스트/메타 분리
            ids = []
            texts = []
            metas = []
            for idx, c in enumerate(chunks):
                cid = make_chunk_id(pdf_path_obj, c["page"], idx)
                ids.append(cid)
                texts.append(c["text"])
                metas.append({
                    "source": c["source"],
                    "page": c["page"],
                    "article": c["article"],
                    "category": cat,
                })

            # 배치 임베딩
            embeddings = model.encode(
                texts,
                batch_size=EMBED_BATCH,
                show_progress_bar=False,
                convert_to_numpy=True,
                normalize_embeddings=True,
            ).tolist()

            # ChromaDB upsert
            try:
                collection.upsert(
                    ids=ids, documents=texts, metadatas=metas, embeddings=embeddings,
                )
            except Exception as exc:
                errors += 1
                log(f"  ! upsert 실패 {pdf_path_obj.name}: {exc}")
                continue

            manifest.upsert(FileEntry(
                path=path_str, category=cat,
                collection=collection_name,
                sha256=sha, size=size, mtime=mtime,
                chunk_ids=ids, chunk_count=len(ids),
                indexed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ))
            files_done += 1
            chunks_total += len(ids)

            if files_done % progress_every == 0:
                log(f"  진행 {files_done}/{total} (chunks {chunks_total})")
                manifest.save()  # 중간 세이브

    return files_done, chunks_total, errors


def cycle(manifest: Manifest, client, model: SentenceTransformer) -> dict:
    t0 = time.time()
    pending, deleted = detect_changes(manifest)
    removed = remove_deleted(manifest, client, deleted) if deleted else 0
    if deleted:
        log(f"  삭제 감지: 파일 {len(deleted)}개 / 청크 {removed}개 제거")
    if pending:
        log(f"  처리 대상: 파일 {len(pending)}개 (workers={PARSE_WORKERS})")
        files, chunks, errs = index_files(manifest, client, model, pending)
        manifest.save()
        return {
            "files_processed": files, "chunks_added": chunks, "errors": errs,
            "deleted_files": len(deleted), "removed_chunks": removed,
            "elapsed_sec": time.time() - t0,
        }
    return {
        "files_processed": 0, "chunks_added": 0, "errors": 0,
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

    log("임베딩 모델 로딩...")
    model = SentenceTransformer(EMBEDDING_MODEL)
    log("ChromaDB 클라이언트 초기화...")
    CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(
        path=str(CHROMA_DIR),
        settings=Settings(anonymized_telemetry=False),
    )
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
