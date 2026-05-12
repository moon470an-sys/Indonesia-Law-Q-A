"""
32,965개 정상 PDF를 PyMuPDF + 새 chunking 으로 재추출 → JSON Lines.

병렬 처리: ProcessPoolExecutor 6워커.
출력: D:\\rag_data\\chunks_v2.jsonl (한 줄당 한 청크).

각 청크 라인:
{
  "id": "<pdf_stem>-p<page>-<idx>",
  "text": "...",
  "metadata": {
    "source": "filename.pdf",
    "page": 5,
    "article": "Pasal 25",
    "category": "헌법",
    "collection": "v2_indonesia_constitution"
  }
}

실패 PDF는 별도 D:\\rag_data\\chunks_v2_failed.txt 에 한 줄씩 기록.

진행:
    python scripts/reextract_all.py [--limit N] [--category 헌법]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

# 프로젝트 루트 import path 확보
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from index_manager import discover_pdfs, normalize_category, Manifest, MAX_PDF_BYTES

OUTPUT_JSONL = Path(r"D:\rag_data\chunks_v2.jsonl")
OUTPUT_FAILED = Path(r"D:\rag_data\chunks_v2_failed.txt")
OUTPUT_STATS = Path(r"D:\rag_data\chunks_v2_stats.json")

# normalize_category 결과 → v2 컬렉션명
V2_PREFIX = "v2_"


def _v2_collection(category: str) -> str:
    return V2_PREFIX + normalize_category(category)


def worker_extract(item: tuple[str, str]) -> dict:
    """워커: (pdf_path_str, category) → 청크 리스트 또는 error."""
    # 워커 import는 각 프로세스에서. PyMuPDF 등은 lazy.
    import sys
    from pathlib import Path
    ROOT = Path(__file__).resolve().parent.parent
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from scripts.extract_v2 import extract_pdf_v2

    pdf_path_str, category = item
    pdf_path = Path(pdf_path_str)
    try:
        chunks = extract_pdf_v2(pdf_path, category=category)
        return {
            "path": pdf_path_str,
            "category": category,
            "chunks": [
                {
                    "text": c.text,
                    "source": c.source,
                    "page": c.page,
                    "article": c.article,
                    "category": c.category,
                }
                for c in chunks
            ],
        }
    except Exception as exc:
        return {
            "path": pdf_path_str,
            "category": category,
            "error": f"{type(exc).__name__}: {exc}",
        }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="처리할 PDF 수 제한 (테스트용)")
    ap.add_argument("--category", default="", help="특정 카테고리만")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--out", default=str(OUTPUT_JSONL))
    ap.add_argument("--failed", default=str(OUTPUT_FAILED))
    ap.add_argument("--stats", default=str(OUTPUT_STATS))
    ap.add_argument("--skip-oversize", action="store_true",
                    help=f"oversize ({MAX_PDF_BYTES//1024//1024}MB) PDF 스킵")
    ap.add_argument("--skip-failed", action="store_true",
                    help="기존 manifest에서 chunk_count==0 인 entry 스킵")
    args = ap.parse_args()

    out_path = Path(args.out)
    failed_path = Path(args.failed)
    stats_path = Path(args.stats)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # PDF 목록 수집
    print(f"[1/3] PDF 스캔...")
    all_pdfs = list(discover_pdfs())
    if args.category:
        all_pdfs = [(c, p) for c, p in all_pdfs if c == args.category]
    print(f"  발견: {len(all_pdfs)}개")

    # manifest 로드 (failed/oversize 필터링용)
    manifest = Manifest() if (args.skip_oversize or args.skip_failed) else None
    skipped_oversize = 0
    skipped_failed = 0
    work_items: list[tuple[str, str]] = []
    for category, pdf_path in all_pdfs:
        try:
            size = pdf_path.stat().st_size
        except OSError:
            continue
        if args.skip_oversize and size > MAX_PDF_BYTES:
            skipped_oversize += 1
            continue
        if args.skip_failed and manifest is not None:
            e = manifest.entries.get(str(pdf_path))
            if e and e.chunk_count == 0:
                skipped_failed += 1
                continue
        work_items.append((str(pdf_path), category))

    if args.limit > 0:
        work_items = work_items[: args.limit]

    total = len(work_items)
    print(f"  처리 대상: {total}개 (oversize 스킵 {skipped_oversize}, "
          f"failed 스킵 {skipped_failed})")

    # 기존 output 백업
    if out_path.exists():
        bak = out_path.with_suffix(".jsonl.bak")
        print(f"  기존 {out_path} → {bak}")
        out_path.replace(bak)

    print(f"[2/3] 추출 시작 (workers={args.workers})")
    t_start = time.time()
    files_ok = 0
    files_err = 0
    chunks_total = 0
    chunks_by_collection: dict[str, int] = {}

    # 진행률 표시 단위
    progress_every = max(50, total // 100)
    last_log_time = t_start
    last_log_files = 0

    with open(out_path, "w", encoding="utf-8") as f_out, \
         open(failed_path, "w", encoding="utf-8") as f_fail:

        with ProcessPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(worker_extract, item): item for item in work_items}
            for fut in as_completed(futures):
                pdf_path_str, category = futures[fut]
                try:
                    res = fut.result()
                except Exception as exc:
                    files_err += 1
                    f_fail.write(f"{pdf_path_str}\tpool_exception: {type(exc).__name__}: {exc}\n")
                    continue

                if "error" in res:
                    files_err += 1
                    f_fail.write(f"{pdf_path_str}\t{res['error']}\n")
                    continue

                col_name = _v2_collection(res["category"])
                pdf_stem = Path(pdf_path_str).stem
                for idx, c in enumerate(res["chunks"]):
                    chunk_id = f"{pdf_stem}-p{c['page']}-{idx}"
                    line = {
                        "id": chunk_id,
                        "text": c["text"],
                        "metadata": {
                            "source": c["source"],
                            "page": c["page"],
                            "article": c["article"],
                            "category": c["category"],
                            "collection": col_name,
                        },
                    }
                    f_out.write(json.dumps(line, ensure_ascii=False) + "\n")
                    chunks_total += 1
                    chunks_by_collection[col_name] = chunks_by_collection.get(col_name, 0) + 1

                files_ok += 1

                done = files_ok + files_err
                if done % progress_every == 0 or done == total:
                    now = time.time()
                    rate = (done - last_log_files) / max(0.1, now - last_log_time)
                    last_log_time = now
                    last_log_files = done
                    eta_sec = (total - done) / max(0.1, rate) if rate > 0 else 0
                    print(f"  진행 {done}/{total} (ok {files_ok}, err {files_err}, "
                          f"chunks {chunks_total}, {rate:.1f}f/s, ETA {eta_sec/60:.1f}min)",
                          flush=True)

    elapsed = time.time() - t_start
    print(f"[3/3] 완료: {files_ok} ok / {files_err} err / {chunks_total} chunks / "
          f"{elapsed:.0f}s ({elapsed/60:.1f}min)")

    stats = {
        "total_pdfs": total,
        "files_ok": files_ok,
        "files_err": files_err,
        "chunks_total": chunks_total,
        "chunks_by_collection": chunks_by_collection,
        "elapsed_sec": elapsed,
        "output_jsonl": str(out_path),
        "output_failed": str(failed_path),
        "skipped_oversize": skipped_oversize,
        "skipped_failed": skipped_failed,
    }
    stats_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  stats → {stats_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
