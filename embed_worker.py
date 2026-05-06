"""
파싱+임베딩을 한 워커 프로세스에서 처리하는 모듈.

ProcessPoolExecutor가 import할 수 있도록 module-level 함수로 노출.
각 워커 프로세스는 자기 모델 사본을 로드하고 in-process 캐시(_model_singleton)에 보관.
OMP_NUM_THREADS=1로 강제해 워커 간 BLAS 컨텐션을 방지.
"""
from __future__ import annotations

import os
import sys

# 워커는 BLAS/MKL이 1코어만 쓰도록 강제 (워커 N개 × 1스레드 = 깨끗한 분할)
# 반드시 sentence-transformers/torch import 전에 설정해야 함
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

# 모델/캐시 위치 통일
_DEFAULT_CACHE = os.getenv("RAG_MODEL_CACHE", r"D:\hf_cache")
os.environ.setdefault("HF_HOME", _DEFAULT_CACHE)
os.environ.setdefault("HUGGINGFACE_HUB_CACHE", os.path.join(_DEFAULT_CACHE, "hub"))
os.environ.setdefault("TRANSFORMERS_CACHE", os.path.join(_DEFAULT_CACHE, "transformers"))
os.environ.setdefault("SENTENCE_TRANSFORMERS_HOME", os.path.join(_DEFAULT_CACHE, "sentence_transformers"))
os.environ.setdefault("TORCH_HOME", os.path.join(_DEFAULT_CACHE, "torch"))


_MODEL_NAME = os.getenv(
    "RAG_EMBEDDING_MODEL",
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
)
_EMBED_BATCH = int(os.getenv("RAG_EMBED_BATCH", "128"))
_PARSE_TIMEOUT = int(os.getenv("RAG_PARSE_TIMEOUT", "60"))

# 워커 프로세스 내 글로벌 모델 (한 번만 로드)
_model_singleton = None


def _get_model():
    global _model_singleton
    if _model_singleton is None:
        # 콘솔 인코딩 fix
        try:
            if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            if sys.stderr is not None and hasattr(sys.stderr, "reconfigure"):
                sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
        # 시끄러운 라이브러리 로그 억제
        import logging
        for name in ("pdfminer", "pypdf", "sentence_transformers", "transformers"):
            logging.getLogger(name).setLevel(logging.ERROR)

        from sentence_transformers import SentenceTransformer
        _model_singleton = SentenceTransformer(_MODEL_NAME)
    return _model_singleton


def init_worker():
    """ProcessPoolExecutor의 initializer로 사용. 모델을 미리 로드해 첫 호출 지연을 없앰."""
    _get_model()


def parse_and_embed(args: tuple[str, str]) -> dict:
    """ProcessPoolExecutor 워커 함수.

    args: (pdf_path_str, category)
    returns: {
        "path": str,
        "category": str,
        "ids": [...], "texts": [...], "metas": [...], "embeddings": [...]
    } or {"path": ..., "error": "..."}
    """
    pdf_path_str, category = args
    try:
        # parse_pdf은 index_manager에서 가져옴 (이미 인코딩 안전 처리됨)
        from index_manager import parse_pdf, make_chunk_id, normalize_category
        from pathlib import Path
        pdf_path = Path(pdf_path_str)
        chunks = parse_pdf(pdf_path)
    except Exception as exc:
        return {"path": pdf_path_str, "error": f"{type(exc).__name__}: {exc}"}

    if not chunks:
        return {"path": pdf_path_str, "category": category,
                "ids": [], "texts": [], "metas": [], "embeddings": []}
    if isinstance(chunks[0], dict) and chunks[0].get("_error"):
        return {"path": pdf_path_str, "error": chunks[0]["_error"]}

    ids: list[str] = []
    texts: list[str] = []
    metas: list[dict] = []
    for idx, c in enumerate(chunks):
        ids.append(make_chunk_id(pdf_path, c["page"], idx))
        texts.append(c["text"])
        metas.append({
            "source": c["source"],
            "page": c["page"],
            "article": c["article"],
            "category": category,
        })

    try:
        model = _get_model()
        embs = model.encode(
            texts,
            batch_size=_EMBED_BATCH,
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=True,
        ).tolist()
    except Exception as exc:
        return {"path": pdf_path_str, "error": f"embed:{type(exc).__name__}: {exc}"}

    return {
        "path": pdf_path_str,
        "category": category,
        "collection": normalize_category(category),
        "ids": ids,
        "texts": texts,
        "metas": metas,
        "embeddings": embs,
    }
