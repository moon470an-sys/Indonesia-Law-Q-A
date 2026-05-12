"""
RunPod GPU 인스턴스에서 실행하는 BGE-M3 임베딩 스크립트.

기존 FlagEmbedding 의존성 제거 (transformers.trainer 호환성 문제 회피) —
sentence-transformers 만 사용해서 dense embedding 추출.

입력: chunks_v2.jsonl
출력:
  - chunks_v2.embeddings.npy   (float16, shape=(N, 1024))
  - chunks_v2.ids.txt          (한 줄당 한 id, 순서 일치)
  - chunks_v2.embeddings.meta.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np


def load_chunks(jsonl_path: Path):
    ids: list[str] = []
    texts: list[str] = []
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            ids.append(obj["id"])
            texts.append(obj["text"])
    return ids, texts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--model", default="BAAI/bge-m3")
    ap.add_argument("--batch-size", type=int, default=128)
    ap.add_argument("--max-length", type=int, default=512)
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--fp16", action="store_true", default=True)
    ap.add_argument("--chunk-save-every", type=int, default=50000)
    args = ap.parse_args()

    jsonl_path = Path(args.jsonl)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[1/4] 청크 로드: {jsonl_path}", flush=True)
    t0 = time.time()
    ids, texts = load_chunks(jsonl_path)
    print(f"  {len(ids)}개 로드 ({time.time()-t0:.1f}s)", flush=True)
    if not ids:
        return 1

    ids_path = out_dir / "chunks_v2.ids.txt"
    ids_path.write_text("\n".join(ids), encoding="utf-8")
    print(f"  ids → {ids_path}", flush=True)

    print(f"[2/4] 모델 로드: {args.model} (device={args.device}, fp16={args.fp16})", flush=True)
    t1 = time.time()
    import torch
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(args.model, device=args.device)
    if args.fp16 and args.device == "cuda":
        model.half()
    model.max_seq_length = args.max_length
    print(f"  모델 OK ({time.time()-t1:.1f}s)", flush=True)

    print(f"[3/4] 임베딩 시작 (batch={args.batch_size}, max_len={args.max_length})", flush=True)
    t2 = time.time()
    all_emb = np.empty((len(ids), 1024), dtype=np.float16)

    pos = 0
    while pos < len(ids):
        end = min(pos + args.chunk_save_every, len(ids))
        sub_texts = texts[pos:end]
        t_batch = time.time()
        emb = model.encode(
            sub_texts,
            batch_size=args.batch_size,
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )
        all_emb[pos:end] = emb.astype(np.float16)
        elapsed = time.time() - t_batch
        rate = (end - pos) / max(0.1, elapsed)
        eta = (len(ids) - end) / max(0.1, rate)
        print(f"  [{end}/{len(ids)}] +{end-pos} chunks in {elapsed:.0f}s "
              f"({rate:.0f} chunks/s, ETA {eta/60:.1f}min)", flush=True)

        # partial save
        np.save(out_dir / "chunks_v2.embeddings.partial.npy", all_emb[:end])
        pos = end

    final_path = out_dir / "chunks_v2.embeddings.npy"
    np.save(final_path, all_emb)
    partial_path = out_dir / "chunks_v2.embeddings.partial.npy"
    if partial_path.exists():
        partial_path.unlink()

    total = time.time() - t2
    print(f"[4/4] 완료: {len(ids)} embeddings → {final_path}", flush=True)
    print(f"  shape={all_emb.shape}, dtype={all_emb.dtype}, "
          f"size={final_path.stat().st_size/1024/1024:.1f}MB", flush=True)
    print(f"  총 {total:.0f}s ({total/60:.1f}min, {len(ids)/total:.0f} chunks/s)", flush=True)

    meta = {
        "model": args.model,
        "dim": 1024,
        "dtype": "float16",
        "total_chunks": len(ids),
        "elapsed_sec": total,
        "rate_chunks_per_sec": len(ids) / total,
        "batch_size": args.batch_size,
        "max_length": args.max_length,
        "library": "sentence-transformers",
    }
    (out_dir / "chunks_v2.embeddings.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
