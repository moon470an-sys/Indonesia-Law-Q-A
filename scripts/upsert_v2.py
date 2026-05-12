"""
임베딩 결과(embeddings.npy + ids.txt)와 JSON Lines(chunks_v2.jsonl)를 결합해
ChromaDB v2_indonesia_* 컬렉션에 upsert.

기존 indonesia_* 컬렉션은 건드리지 않음 (atomic switch는 별도).

실행:
    python scripts/upsert_v2.py \
        --jsonl D:\\rag_data\\chunks_v2.jsonl \
        --embeddings D:\\rag_data\\embeddings\\chunks_v2.embeddings.npy \
        --ids D:\\rag_data\\embeddings\\chunks_v2.ids.txt
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rag_chroma import get_chroma_client, describe_target

UPSERT_BATCH = 5000  # ChromaDB max_batch_size(5461) 미만


def load_metadata(jsonl_path: Path) -> dict[str, dict]:
    """id → (text, metadata) 매핑."""
    out: dict[str, dict] = {}
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            out[obj["id"]] = {
                "text": obj["text"],
                "metadata": obj["metadata"],
            }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl", required=True)
    ap.add_argument("--embeddings", required=True, help=".npy 파일")
    ap.add_argument("--ids", required=True, help="ids.txt — embeddings 행 순서와 일치")
    ap.add_argument("--collection-prefix", default="v2_", help="새 컬렉션 prefix")
    ap.add_argument("--reset", action="store_true",
                    help="대상 v2_ 컬렉션을 먼저 비움 (재실행 시)")
    args = ap.parse_args()

    print(f"[1/5] 메타데이터 로드: {args.jsonl}")
    t0 = time.time()
    meta = load_metadata(Path(args.jsonl))
    print(f"  {len(meta)}개 청크 메타 로드 ({time.time()-t0:.1f}s)")

    print(f"[2/5] embeddings 로드: {args.embeddings}")
    t1 = time.time()
    embs = np.load(args.embeddings)
    ids_lines = Path(args.ids).read_text(encoding="utf-8").splitlines()
    ids = [s for s in ids_lines if s.strip()]
    if len(ids) != len(embs):
        print(f"FATAL: ids({len(ids)}) != embeddings({len(embs)})")
        return 1
    print(f"  shape={embs.shape}, dtype={embs.dtype}, ({time.time()-t1:.1f}s)")

    # 누락 검사
    missing = [i for i in ids if i not in meta]
    if missing:
        print(f"WARN: meta에 없는 id {len(missing)}개. 첫 5: {missing[:5]}")

    # 컬렉션별 그룹핑
    print(f"[3/5] 컬렉션별 그룹핑")
    by_col: dict[str, list[int]] = {}  # collection → row indices
    for idx, chunk_id in enumerate(ids):
        m = meta.get(chunk_id)
        if not m:
            continue
        col = m["metadata"].get("collection") or "v2_indonesia_lainnya"
        by_col.setdefault(col, []).append(idx)
    print(f"  컬렉션 {len(by_col)}개:")
    for col, rows in sorted(by_col.items(), key=lambda kv: -len(kv[1])):
        print(f"    {col}: {len(rows)} chunks")

    # ChromaDB 클라이언트
    print(f"[4/5] ChromaDB 연결: {describe_target()}")
    client = get_chroma_client()

    if args.reset:
        for col_name in by_col:
            try:
                client.delete_collection(col_name)
                print(f"  reset: {col_name} deleted")
            except Exception:
                pass

    # 각 컬렉션에 upsert
    print(f"[5/5] upsert 시작")
    t_total = time.time()
    for col_name, rows in by_col.items():
        try:
            col = client.get_collection(col_name)
        except Exception:
            col = client.create_collection(name=col_name, metadata={"hnsw:space": "cosine"})
        n = len(rows)
        t_col = time.time()

        # ChromaDB는 list of lists embeddings 받음. float16 → float32 변환 필요 (대부분 client).
        for batch_start in range(0, n, UPSERT_BATCH):
            batch_end = min(batch_start + UPSERT_BATCH, n)
            batch_rows = rows[batch_start:batch_end]
            batch_ids = [ids[r] for r in batch_rows]
            batch_emb = embs[batch_rows].astype(np.float32).tolist()
            batch_docs: list[str] = []
            batch_metas: list[dict] = []
            for r in batch_rows:
                m = meta[ids[r]]
                batch_docs.append(m["text"])
                batch_metas.append(m["metadata"])

            col.upsert(
                ids=batch_ids,
                documents=batch_docs,
                metadatas=batch_metas,
                embeddings=batch_emb,
            )
            done = batch_end
            elapsed = time.time() - t_col
            rate = done / max(0.1, elapsed)
            print(f"  [{col_name}] {done}/{n} ({rate:.0f}/s)", flush=True)

        print(f"  [{col_name}] OK ({n} in {time.time()-t_col:.0f}s)")

    print(f"\n완료: 총 {sum(len(r) for r in by_col.values())} chunks "
          f"in {len(by_col)} collections, {time.time()-t_total:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
