"""
한 batch (30개 Perda 파일) 처리를 단계별로 시간 측정.
병목 진단: parse / embed / upsert / manifest_save 중 어느 단계가 느린지.

각 워커가 (parse_time, embed_time)을 함께 반환하도록 부분 측정,
메인은 upsert/manifest 시간을 측정.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
import json

# Force UTF-8 console
for s in ("stdout", "stderr"):
    f = getattr(sys, s, None)
    if f and hasattr(f, "reconfigure"):
        try: f.reconfigure(encoding="utf-8", errors="replace")
        except Exception: pass

from concurrent.futures import ProcessPoolExecutor, as_completed

import index_manager  # ensures env vars set
from index_manager import (
    SOURCE_ROOT, CHROMA_DIR, MANIFEST_PATH,
    Manifest, FileEntry, normalize_category, make_chunk_id,
)
from rag_chroma import describe_target, get_chroma_client

PARSE_WORKERS = int(os.getenv("RAG_PARSE_WORKERS", "6"))
EMBED_BATCH = int(os.getenv("RAG_EMBED_BATCH", "128"))
SAMPLE_SIZE = int(os.getenv("PROFILE_N", "30"))


def parse_and_embed_timed(args):
    """parse_pdf + embed, 단계별 시간 반환."""
    pdf_path_str, category = args
    t0 = time.perf_counter()
    from index_manager import parse_pdf
    pdf_path = Path(pdf_path_str)
    chunks = parse_pdf(pdf_path)
    t_parse = time.perf_counter() - t0

    if not chunks or (isinstance(chunks[0], dict) and chunks[0].get("_error")):
        return {
            "path": pdf_path_str, "error": chunks[0].get("_error", "no_chunks") if chunks else "no_chunks",
            "t_parse": t_parse, "t_embed": 0.0, "n_chunks": 0, "n_pages": 0,
        }

    pages = sorted({c["page"] for c in chunks})
    ids, texts, metas = [], [], []
    for idx, c in enumerate(chunks):
        ids.append(make_chunk_id(pdf_path, c["page"], idx))
        texts.append(c["text"])
        metas.append({"source": c["source"], "page": c["page"],
                      "article": c["article"], "category": category})

    t1 = time.perf_counter()
    from embed_worker import _get_model
    model = _get_model()
    embs = model.encode(texts, batch_size=EMBED_BATCH, show_progress_bar=False,
                        convert_to_numpy=True, normalize_embeddings=True).tolist()
    t_embed = time.perf_counter() - t1

    return {
        "path": pdf_path_str, "category": category,
        "collection": normalize_category(category),
        "ids": ids, "texts": texts, "metas": metas, "embeddings": embs,
        "t_parse": t_parse, "t_embed": t_embed,
        "n_chunks": len(ids), "n_pages": len(pages),
        "size_mb": pdf_path.stat().st_size / 1024 / 1024,
    }


def init_worker():
    from embed_worker import _get_model
    _get_model()


def main():
    print(f"=== Profile: {SAMPLE_SIZE} Perda files, workers={PARSE_WORKERS} ===")
    manifest = Manifest()
    print(f"Manifest entries: {len(manifest.entries)}")

    # 이미 OK된 Perda 파일 (chunk_count > 0)을 샘플로 — 재처리해도 ChromaDB upsert는 idempotent.
    # 실제 성공 케이스의 처리 시간을 측정하려는 의도.
    candidates = []
    for path, e in manifest.entries.items():
        if e.category != "지방조례_Perda" or e.chunk_count == 0:
            continue
        if not Path(path).exists():
            continue
        candidates.append((path, e.category))
        if len(candidates) >= SAMPLE_SIZE:
            break

    print(f"샘플 {len(candidates)}개 선택 (이미 OK된 Perda 재처리)")

    # ChromaDB
    t = time.perf_counter()
    client = get_chroma_client()
    print(f"  ChromaDB client init: {time.perf_counter()-t:.2f}s (target={describe_target()})")

    # Submit
    t_total_start = time.perf_counter()
    print("\n--- Phase 1: ProcessPool parse+embed ---")
    results = []
    t_pool_start = time.perf_counter()
    with ProcessPoolExecutor(max_workers=PARSE_WORKERS, initializer=init_worker) as pool:
        t_init_done = time.perf_counter()
        print(f"  Pool initialized (workers loaded models): {t_init_done - t_pool_start:.2f}s")
        futures = [pool.submit(parse_and_embed_timed, c) for c in candidates]
        first_result_t = None
        for fut in as_completed(futures):
            if first_result_t is None:
                first_result_t = time.perf_counter()
            results.append(fut.result())
    t_pool_end = time.perf_counter()
    print(f"  Pool processing wall time: {t_pool_end - t_init_done:.2f}s")
    print(f"  First result: {first_result_t - t_init_done:.2f}s after pool init")

    # 통계
    ok = [r for r in results if "error" not in r]
    err = [r for r in results if "error" in r]
    total_chunks = sum(r["n_chunks"] for r in ok)
    total_pages = sum(r["n_pages"] for r in ok)
    sum_parse = sum(r["t_parse"] for r in results)
    sum_embed = sum(r["t_embed"] for r in results)
    avg_size = sum(r.get("size_mb", 0) for r in ok) / max(1, len(ok))

    print(f"\n  성공: {len(ok)}, 에러: {len(err)}")
    print(f"  총 청크: {total_chunks}, 총 페이지: {total_pages}")
    print(f"  평균 파일 크기: {avg_size:.2f} MB")
    print(f"  CPU 시간: parse={sum_parse:.1f}s, embed={sum_embed:.1f}s (총 {sum_parse+sum_embed:.1f}s)")
    print(f"  Wall time per file: {(t_pool_end - t_init_done)/len(candidates):.2f}s")
    print(f"  CPU time per file: parse={sum_parse/len(candidates):.2f}s, embed={sum_embed/len(candidates):.2f}s")

    if total_pages:
        print(f"  ms/page: parse={sum_parse*1000/total_pages:.1f}, embed={sum_embed*1000/total_pages:.1f}")

    # Phase 2: ChromaDB upsert
    print("\n--- Phase 2: ChromaDB upsert ---")
    groups = {}
    for r in ok:
        col = r["collection"]
        g = groups.setdefault(col, {"ids": [], "texts": [], "metas": [], "embs": []})
        g["ids"].extend(r["ids"]); g["texts"].extend(r["texts"])
        g["metas"].extend(r["metas"]); g["embs"].extend(r["embeddings"])

    t_upsert_start = time.perf_counter()
    for col_name, g in groups.items():
        col = client.get_or_create_collection(col_name, metadata={"hnsw:space": "cosine"})
        n = len(g["ids"])
        # 5000 단위 분할
        BATCH = 5000
        for i in range(0, n, BATCH):
            j = min(i + BATCH, n)
            t_b = time.perf_counter()
            col.upsert(ids=g["ids"][i:j], documents=g["texts"][i:j],
                       metadatas=g["metas"][i:j], embeddings=g["embs"][i:j])
            print(f"  upsert {col_name} [{i}:{j}] ({j-i} chunks): {time.perf_counter()-t_b:.2f}s")
    t_upsert = time.perf_counter() - t_upsert_start
    print(f"  Total upsert: {t_upsert:.2f}s for {total_chunks} chunks ({total_chunks/max(0.01,t_upsert):.0f} chunks/s)")

    # Phase 3: manifest save (test)
    print("\n--- Phase 3: manifest save ---")
    # Write 30 fake entries to manifest copy
    from datetime import datetime, timezone
    for r in ok:
        manifest.upsert(FileEntry(
            path=r["path"], category=r["category"], collection=r["collection"],
            sha256="profile_dummy", size=int(r.get("size_mb",0)*1024*1024), mtime=time.time(),
            chunk_ids=r["ids"], chunk_count=len(r["ids"]),
            indexed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ))
    # NOTE: we don't actually save to avoid corrupting prod manifest. Just measure serialization+write to /tmp.
    import tempfile
    tmpdir = tempfile.mkdtemp()
    tmp_path = Path(tmpdir) / "manifest_test.json"
    from dataclasses import asdict
    t = time.perf_counter()
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump({k: asdict(v) for k, v in manifest.entries.items()}, f, ensure_ascii=False, indent=0)
    t_save = time.perf_counter() - t
    sz_mb = tmp_path.stat().st_size / 1024 / 1024
    print(f"  manifest serialize+write ({len(manifest.entries)} entries, {sz_mb:.1f}MB): {t_save:.2f}s")
    tmp_path.unlink(); Path(tmpdir).rmdir()

    print("\n=== Summary ===")
    wall = t_pool_end - t_total_start
    print(f"Wall time (parse+embed): {t_pool_end-t_init_done:.1f}s for {len(candidates)} files")
    print(f"Wall time per file: {(t_pool_end-t_init_done)/len(candidates):.2f}s")
    print(f"Theoretical {PARSE_WORKERS}-worker rate: {PARSE_WORKERS*60/((t_pool_end-t_init_done)/len(candidates)*PARSE_WORKERS):.1f} files/min")
    print(f"Upsert: {t_upsert:.1f}s ({t_upsert/wall*100:.0f}% of wall)")
    print(f"Manifest save: {t_save:.2f}s (per-flush cost)")
    print(f"Init overhead (model load): {t_init_done - t_pool_start:.1f}s (one-time)")


if __name__ == "__main__":
    main()
