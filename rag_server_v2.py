"""
인도네시아 법령 RAG v2 — BGE-M3 + Claude query expansion + Claude reranker.

기존 rag_server.py 와 격리. v1 (MiniLM 384d, indonesia_*) 는 그대로 두고
v2 (BGE-M3 1024d, v2_indonesia_*) 는 별도 포트 8002로 운영.

검증 후 atomic switch 시 watchdog가 v2를 8000으로 띄우도록 변경.

실행:
    uvicorn rag_server_v2:app --host 127.0.0.1 --port 8002
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# HF 캐시 위치 (D 드라이브)
_DEFAULT_CACHE = Path(os.getenv("RAG_MODEL_CACHE", r"D:\hf_cache"))
_DEFAULT_CACHE.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("HF_HOME", str(_DEFAULT_CACHE))
os.environ.setdefault("HUGGINGFACE_HUB_CACHE", str(_DEFAULT_CACHE / "hub"))
os.environ.setdefault("TRANSFORMERS_CACHE", str(_DEFAULT_CACHE / "transformers"))
os.environ.setdefault("SENTENCE_TRANSFORMERS_HOME", str(_DEFAULT_CACHE / "sentence_transformers"))
os.environ.setdefault("TORCH_HOME", str(_DEFAULT_CACHE / "torch"))

from anthropic import Anthropic, RateLimitError
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env")

# ChromaDB
from rag_chroma import (
    CHROMA_MODE,
    CHROMA_PATH as CHROMA_DIR,
    describe_target,
    get_chroma_client,
)

# === v2 설정 ===
COLLECTION_PREFIX = "v2_indonesia_"
EMBEDDING_MODEL = os.getenv("RAG_V2_EMBEDDING_MODEL", "BAAI/bge-m3")
EMBEDDING_DIM = 1024
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
CLAUDE_HAIKU = os.getenv("CLAUDE_HAIKU", "claude-haiku-4-5-20251001")

TOP_K_DEFAULT = 12
# 메인 답변용 user_message에 인라인되는 청크 본문 길이 cap.
# 청크 전문이 필요하면 모델이 fetch_article_chunks로 끌어옴 → 매 호출 입력 토큰 절감.
CONTEXT_CHUNK_TEXT_MAX = 1500
FETCH_PER_QUERY = 30        # 각 query embedding당 가져올 후보 수
RERANK_POOL_SIZE = 50       # reranker에 보낼 후보 수
MAX_ANSWER_TOKENS = 8192    # 비-interleaved 경로의 thinking+답변 합. thinking 2000 + 답변 ~6000.
MAX_ANSWER_TOKENS_INTERLEAVED = 16000  # interleaved thinking + agent loop는 thinking 자주 발생 → 여유.
# Extended thinking — Sonnet 4.6/Opus 4.7에서 답변 전 추론 단계 명시적으로 사용.
# 위계 충돌, 복합 비교, 다중 인용 같은 복잡 질문에서 답변 깊이가 크게 개선됨.
THINKING_BUDGET_TOKENS = 2000
# Interleaved thinking (Phase 11): tool_use 사이사이 thinking block 허용 → 도구 결과 보고
# 다음 행동을 추론. multi-hop 법령 추적·위임 분석에 효과적.
THINKING_BUDGET_INTERLEAVED = 5000
INTERLEAVED_BETA = "interleaved-thinking-2025-05-14"

# 위계 가중치 (cosine distance에 곱하기)
HIERARCHY_WEIGHTS = {
    "v2_indonesia_constitution": 0.80,
    "v2_indonesia_uu": 0.88,
    "v2_indonesia_pp": 0.92,
    "v2_indonesia_perpres": 0.94,
    "v2_indonesia_permen": 0.96,
    "v2_indonesia_kepmen": 0.97,
    "v2_indonesia_perda": 0.97,
    "v2_indonesia_lainnya": 1.00,
}
DEFAULT_HIERARCHY_WEIGHT = 1.0

# Page 페널티 (도입부 후순위)
EARLY_PAGE_THRESHOLD = 2
EARLY_PAGE_PENALTY = 1.10

# === Hybrid retrieval (Phase 10: BM25 + RRF + MMR) ===
# 인니 법령 ID(UU 13/2003, Pasal 7) 정확 매칭은 dense보다 BM25가 강함.
# Reciprocal Rank Fusion(RRF)로 dense·BM25 ranks를 합치고,
# Maximal Marginal Relevance(MMR)로 같은 source/article 중복 청크를 줄임.
BM25_DIR = Path(os.getenv("RAG_BM25_DIR", r"D:\rag_data\bm25"))
BM25_DIR.mkdir(parents=True, exist_ok=True)
RRF_K = 60                # RRF 상수 (검색 분야 표준값)
MMR_LAMBDA = 0.7          # MMR relevance vs diversity 가중치
BM25_FETCH_MULTIPLIER = 2 # BM25는 dense보다 후보 폭을 더 잡고 RRF에서 자연스레 솎임

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.getenv(
        "ALLOWED_ORIGINS",
        "https://moon470an-sys.github.io,http://localhost:8501,http://127.0.0.1:8501",
    ).split(",")
    if o.strip()
]

# 토큰 검증 정책 (v1과 동일 방식)
_config: dict[str, Any] = {}
_config_lock = threading.Lock()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# Phase 11 feature flags — 모두 env로 토글 가능. 기본은 권장 on.
INTERLEAVED_ENABLED = _env_bool("RAG_INTERLEAVED_THINKING", default=True)
CROSS_ENCODER_ENABLED = _env_bool("RAG_USE_CROSS_ENCODER", default=True)
CROSS_ENCODER_MODEL = os.getenv("RAG_CROSS_ENCODER_MODEL", "BAAI/bge-reranker-v2-m3")
CROSS_ENCODER_CUT = int(os.getenv("RAG_CROSS_ENCODER_CUT", "20"))  # Haiku에 보낼 컷
ADV_CRITIQUE_ENABLED = _env_bool("RAG_ADVERSARIAL_CRITIQUE", default=True)
HIERARCHICAL_ENABLED = _env_bool("RAG_HIERARCHICAL_RETRIEVAL", default=True)


def _read_policy_env() -> dict[str, Any]:
    tokens_raw = os.getenv("RAG_TOKENS") or os.getenv("CLIENT_API_TOKEN") or ""
    tokens = {t.strip() for t in tokens_raw.split(",") if t.strip()}
    require = _env_bool("RAG_REQUIRE_TOKEN", default=bool(tokens))
    return {"require_token": require, "tokens": frozenset(tokens)}


def reload_config() -> dict[str, Any]:
    load_dotenv(PROJECT_DIR / ".env", override=True)
    new_cfg = _read_policy_env()
    with _config_lock:
        for k, v in new_cfg.items():
            _config[k] = v
    return {
        "reloaded": list(new_cfg.keys()),
        "current": {
            "require_token": _config["require_token"],
            "token_count": len(_config["tokens"]),
        },
    }


reload_config()
ADMIN_KEY = os.getenv("RAG_ADMIN_KEY", "")

app = FastAPI(title="Indonesia RAG v2 (BGE-M3 + Claude reranker)", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    allow_credentials=False,
)


def require_token(x_api_token: str | None = Header(default=None)) -> None:
    with _config_lock:
        require = bool(_config.get("require_token"))
        tokens: frozenset[str] = _config.get("tokens", frozenset())
    if not require:
        return
    if not tokens or x_api_token not in tokens:
        raise HTTPException(status_code=401, detail="invalid token")


_state: dict[str, Any] = {
    "embed_model": None,
    "cross_encoder": None,           # Phase 11: BGE-reranker-v2-m3
    "cross_encoder_failed": False,   # 로딩 1회 실패 시 다시 시도하지 않음
    "chroma_client": None,
    "anthropic": None,
    "health_cache": None,
    "health_cache_ts": 0.0,
    "ready": False,
    "warmup_started": False,
    "warmup_finished_ts": 0.0,
    "readiness_error": None,
}

HEALTH_CACHE_TTL = 86400.0

# ===== 컬렉션별 고유 PDF 수 — manifest 기반 =====
# chroma `col.get(include=["metadatas"])`로 큰 컬렉션(permen 55만+) 전체 메타데이터를
# 끌어오는 건 timeout이 자주 발생해 except → 0으로 캐시되는 사고를 일으켰다.
# ingest manifest에 카테고리별 PDF 정보(chunk_count 포함)가 정확히 있으므로
# 그것을 mtime 기반으로 캐싱해서 사용한다.
MANIFEST_PATH = Path(os.getenv("RAG_MANIFEST_PATH", r"D:\rag_data\manifest.json"))
_manifest_cache: dict[str, Any] = {"mtime": None, "per_collection_docs": {}}


def _load_doc_counts_from_manifest() -> dict[str, int]:
    """manifest.json에서 v2 컬렉션명 기준 chunk_count>0 PDF 수 집계.
    파일 mtime이 바뀐 경우에만 재로딩 (manifest 78MB → 매 호출마다 파싱은 부담)."""
    try:
        st = MANIFEST_PATH.stat()
    except FileNotFoundError:
        return {}
    if _manifest_cache.get("mtime") == st.st_mtime:
        return dict(_manifest_cache.get("per_collection_docs") or {})
    try:
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as exc:
        logger.warning("manifest 로드 실패: %s", exc)
        return dict(_manifest_cache.get("per_collection_docs") or {})
    counts: dict[str, int] = {}
    for v in data.values():
        if not isinstance(v, dict):
            continue
        try:
            cc = int(v.get("chunk_count", 0) or 0)
        except (TypeError, ValueError):
            cc = 0
        if cc <= 0:
            continue
        col = v.get("collection")
        if not col:
            continue
        # manifest는 v1 컬렉션명('indonesia_X'), 라이브 v2 컬렉션은 'v2_indonesia_X'.
        v2_col = col if col.startswith("v2_") else "v2_" + col
        counts[v2_col] = counts.get(v2_col, 0) + 1
    _manifest_cache["mtime"] = st.st_mtime
    _manifest_cache["per_collection_docs"] = counts
    return dict(counts)


def get_embed_model():
    """BGE-M3 로드 (CPU, sentence-transformers). 첫 호출 시 ~2GB 다운로드.

    인덱싱 때와 동일 라이브러리(sentence-transformers)를 써야 임베딩이 같은 분포.
    """
    if _state["embed_model"] is None:
        from sentence_transformers import SentenceTransformer
        logger.info("BGE-M3 모델 로딩 시작 (%s, CPU)", EMBEDDING_MODEL)
        m = SentenceTransformer(EMBEDDING_MODEL, device="cpu")
        m.max_seq_length = 512
        _state["embed_model"] = m
        logger.info("BGE-M3 로딩 완료")
    return _state["embed_model"]


def encode_query(texts: list[str]) -> list[list[float]]:
    """텍스트 리스트 → dense embedding 리스트 (1024d, L2-normalized)."""
    m = get_embed_model()
    emb = m.encode(
        texts,
        batch_size=len(texts),
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )
    return [v.astype("float32").tolist() for v in emb]


def get_cross_encoder():
    """BGE-reranker-v2-m3 lazy load. CPU. ~600MB. 실패 시 None — Claude reranker로 폴백."""
    if not CROSS_ENCODER_ENABLED:
        return None
    if _state["cross_encoder"] is not None:
        return _state["cross_encoder"]
    if _state.get("cross_encoder_failed"):
        return None
    try:
        from sentence_transformers import CrossEncoder
        logger.info("Cross-encoder 로딩 시작 (%s, CPU)", CROSS_ENCODER_MODEL)
        ce = CrossEncoder(CROSS_ENCODER_MODEL, max_length=512, device="cpu")
        _state["cross_encoder"] = ce
        logger.info("Cross-encoder 로딩 완료")
        return ce
    except Exception as exc:
        logger.warning("Cross-encoder 로딩 실패 — Claude reranker only: %s", exc)
        _state["cross_encoder_failed"] = True
        return None


def cross_encoder_score(question: str, candidates: list[dict], cut_to: int) -> list[dict]:
    """Cross-encoder로 candidates 점수화 → 상위 cut_to개 반환. 각 c에 'ce_score' 부여.
    실패 시 candidates를 그대로 cut_to까지 잘라 반환."""
    if not candidates or cut_to <= 0:
        return candidates[:cut_to]
    if len(candidates) <= cut_to:
        for c in candidates:
            c.setdefault("ce_score", None)
        return candidates
    ce = get_cross_encoder()
    if ce is None:
        return candidates[:cut_to]
    try:
        pairs = [(question, clean_chunk(c.get("text") or "")[:1500]) for c in candidates]
        scores = ce.predict(pairs, batch_size=16, show_progress_bar=False)
        for c, s in zip(candidates, scores):
            c["ce_score"] = float(s)
        # 내림차순 정렬 후 cut
        candidates.sort(key=lambda c: -(c.get("ce_score") or 0.0))
        return candidates[:cut_to]
    except Exception as exc:
        logger.warning("cross-encoder predict 실패, 원순서로 cut: %s", exc)
        return candidates[:cut_to]


def get_chroma():
    if _state["chroma_client"] is None:
        _state["chroma_client"] = get_chroma_client()
    return _state["chroma_client"]


def list_v2_collections() -> list:
    client = get_chroma()
    return [c for c in client.list_collections() if c.name.startswith(COLLECTION_PREFIX)]


def get_anthropic() -> Anthropic:
    if _state["anthropic"] is None:
        if not ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY 없음")
        # max_retries 2: SDK가 429/503에 exponential backoff로 자동 재시도.
        # 2회면 일시적 1~2초 burst는 흡수하면서, 지속적 ITPM 한도일 때는 빠르게
        # RateLimitError를 던져 event_gen이 rate_limited SSE를 보낼 수 있다.
        # (4회였을 땐 429 retry-after(수십 초)를 4번 기다리며 generator가 분 단위로
        #  멈춰 → SSE 무응답 → 프론트 idle/flat timeout abort. 5분 hang의 주범.)
        _state["anthropic"] = Anthropic(api_key=ANTHROPIC_API_KEY, max_retries=2)
    return _state["anthropic"]


def _agent_loop_kwargs() -> dict:
    """tool_use 답변 생성용 공통 kwargs. interleaved thinking 활성화 시 budget·beta 헤더 적용."""
    if INTERLEAVED_ENABLED:
        return {
            "max_tokens": MAX_ANSWER_TOKENS_INTERLEAVED,
            "thinking": {"type": "enabled", "budget_tokens": THINKING_BUDGET_INTERLEAVED},
            "extra_headers": {"anthropic-beta": INTERLEAVED_BETA},
        }
    return {
        "max_tokens": MAX_ANSWER_TOKENS,
        "thinking": {"type": "enabled", "budget_tokens": THINKING_BUDGET_TOKENS},
    }


# ===== 청크 텍스트 정리 (retrieve 후 안전망) =====

_NOISE_TR = str.maketrans({
    "\xa0": " ",  # nbsp
    "­": "",  # soft hyphen
    "​": "",  # zero-width space
    "‌": "",
    "‍": "",
    "﻿": "",
})


def clean_chunk(text: str) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_NOISE_TR)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


# ===== Phase 10: BM25 인덱스 (lazy build, 디스크 캐시) =====
# 글로벌 인덱스 — 모든 v2 컬렉션을 하나의 BM25에 담아 query당 1회만 평가.
# 디스크 캐시는 fingerprint(총 청크 수) 변경 시 자동 무효화.

_bm25_state: dict[str, Any] = {"global": None}
_bm25_lock = threading.Lock()
_BM25_GLOBAL_PATH = BM25_DIR / "global_v2.pkl"

# 인니 법령 토큰화: 영숫자·한글 단위 + "13/2003" 같은 숫자/연도 슬래시 토큰 보존.
_BM25_TOKEN_RE = re.compile(r"[A-Za-z가-힣]+|\d+(?:[/-]\d+)*")


def _tokenize_bm25(text: str) -> list[str]:
    if not text:
        return []
    return [t.lower() for t in _BM25_TOKEN_RE.findall(text)]


def _build_bm25_global(cols: list, total_count: int) -> dict | None:
    try:
        from rank_bm25 import BM25Okapi
    except ImportError:
        logger.warning("rank_bm25 미설치 — BM25 비활성 (dense-only retrieval)")
        return None
    t0 = time.time()
    ids: list[str] = []
    texts: list[str] = []
    metas: list[dict] = []
    collections: list[str] = []
    for col in cols:
        try:
            r = col.get(include=["documents", "metadatas"])
        except Exception as exc:
            logger.warning("BM25 build: col %s get 실패: %s", col.name, exc)
            continue
        col_ids = r.get("ids") or []
        col_docs = r.get("documents") or []
        col_metas = r.get("metadatas") or []
        for cid, doc, meta in zip(col_ids, col_docs, col_metas):
            ids.append(cid)
            texts.append(doc or "")
            metas.append(meta or {})
            collections.append(col.name)
    if not ids:
        return None
    logger.info("BM25 global 인덱스 토큰화 시작 (n=%d)", len(ids))
    tokens = [_tokenize_bm25(t) for t in texts]
    bm25 = BM25Okapi(tokens)
    idx = {
        "bm25": bm25,
        "ids": ids,
        "texts": texts,
        "metas": metas,
        "collections": collections,
        "count": total_count,
    }
    try:
        import pickle
        with open(_BM25_GLOBAL_PATH, "wb") as f:
            pickle.dump(idx, f)
    except Exception as exc:
        logger.warning("BM25 인덱스 디스크 저장 실패: %s", exc)
    logger.info("BM25 global 인덱스 빌드 완료 (n=%d, %.1fs)", len(ids), time.time() - t0)
    return idx


def get_bm25_global() -> dict | None:
    """글로벌 BM25 인덱스. 메모리/디스크 캐시 → 청크 수 변경 시 재빌드."""
    with _bm25_lock:
        cols = list_v2_collections()
        try:
            total_count = sum(c.count() for c in cols)
        except Exception as exc:
            logger.warning("BM25 fingerprint count 실패: %s", exc)
            return _bm25_state.get("global")
        cached = _bm25_state.get("global")
        if cached and cached.get("count") == total_count:
            return cached
        if _BM25_GLOBAL_PATH.exists():
            try:
                import pickle
                with open(_BM25_GLOBAL_PATH, "rb") as f:
                    disk = pickle.load(f)
                if disk.get("count") == total_count:
                    _bm25_state["global"] = disk
                    return disk
            except Exception as exc:
                logger.warning("BM25 인덱스 디스크 로드 실패: %s", exc)
        return _build_bm25_global(cols, total_count)


def bm25_search_global(query_text: str, top_n: int) -> list[tuple[str, str, dict, str, int]]:
    """글로벌 BM25 검색. 반환: [(chunk_id, text, metadata, collection, bm25_rank), ...]."""
    idx = get_bm25_global()
    if not idx:
        return []
    tokens = _tokenize_bm25(query_text)
    if not tokens:
        return []
    try:
        import numpy as np
    except ImportError:
        return []
    scores = idx["bm25"].get_scores(tokens)
    if len(scores) == 0:
        return []
    n = min(top_n, len(scores))
    # argpartition으로 top-n만 뽑고 그 안에서만 정렬 → 큰 corpus에서 빠름
    part = np.argpartition(-scores, n - 1)[:n] if n < len(scores) else np.arange(len(scores))
    order = part[np.argsort(-scores[part])]
    out: list[tuple[str, str, dict, str, int]] = []
    for rank, pos in enumerate(order):
        s = float(scores[pos])
        if s <= 0:
            break
        i = int(pos)
        out.append((idx["ids"][i], idx["texts"][i], idx["metas"][i], idx["collections"][i], rank))
    return out


# ===== Phase 11: Hierarchical retrieval (document-level 1차) =====
# comparison/hierarchy/case_application intent에서 source(법령 PDF)별 RRF score 집계 →
# top-N source로 후보 한정 → 답변 출처가 흩뿌려지지 않고 핵심 법령에 집중.

_HIERARCHICAL_INTENTS = {"comparison", "hierarchy", "case_application"}
_HIERARCHICAL_TOP_SOURCES = {
    "comparison": 4,
    "hierarchy": 5,
    "case_application": 5,
}
_HIERARCHICAL_MIN_KEEP = 12  # filter 후 너무 적으면 원본 반환


def _hierarchical_filter(candidates: list[dict], intent: str) -> tuple[list[dict], dict]:
    """source별 RRF 합산으로 top-N source 선정 후 그 source의 chunks만 통과시킴.
    intent가 hierarchical 대상이 아니거나 필터링 결과가 너무 적으면 원본 반환.
    """
    info: dict = {"applied": False, "top_sources": [], "reason": ""}
    if not HIERARCHICAL_ENABLED or intent not in _HIERARCHICAL_INTENTS:
        info["reason"] = "disabled_or_intent_skip"
        return candidates, info
    src_scores: dict[str, float] = {}
    for c in candidates:
        src = ((c.get("metadata") or {}).get("source") or "").strip()
        if not src:
            continue
        src_scores[src] = src_scores.get(src, 0.0) + (c.get("rrf_score") or 0.0)
    if not src_scores:
        info["reason"] = "no_sources"
        return candidates, info
    n = _HIERARCHICAL_TOP_SOURCES.get(intent, 4)
    top_sources = [s for s, _ in sorted(src_scores.items(), key=lambda kv: -kv[1])[:n]]
    keep = {s for s in top_sources}
    filtered = [c for c in candidates if ((c.get("metadata") or {}).get("source") or "") in keep]
    if len(filtered) < _HIERARCHICAL_MIN_KEEP:
        info["reason"] = f"too_few_after_filter({len(filtered)})"
        return candidates, info
    info["applied"] = True
    info["top_sources"] = top_sources
    return filtered, info


# ===== MMR (Maximal Marginal Relevance) =====
# rerank 직전 candidates에서 같은 source/Pasal 중복을 솎아내 reranker 입력 다양성 확보.


def _struct_sim(a: dict, b: dict) -> float:
    am = a.get("metadata") or {}
    bm = b.get("metadata") or {}
    sa, sb = am.get("source"), bm.get("source")
    if not sa or not sb or sa != sb:
        return 0.0
    aa = (am.get("article") or "").strip()
    bb = (bm.get("article") or "").strip()
    if aa and bb and aa == bb:
        return 1.0
    return 0.5


def _mmr_select(candidates: list[dict], target_size: int, lambda_: float = MMR_LAMBDA) -> list[dict]:
    if not candidates or target_size <= 0:
        return []
    if len(candidates) <= target_size:
        return candidates
    max_score = max((c.get("rrf_score") or 0.0) for c in candidates) or 1.0
    pool = list(candidates)
    selected: list[dict] = []
    while pool and len(selected) < target_size:
        best_idx = -1
        best_mmr = float("-inf")
        for i, c in enumerate(pool):
            rel = (c.get("rrf_score") or 0.0) / max_score
            max_sim = max((_struct_sim(c, s) for s in selected), default=0.0)
            mmr = lambda_ * rel - (1.0 - lambda_) * max_sim
            if mmr > best_mmr:
                best_mmr = mmr
                best_idx = i
        if best_idx < 0:
            break
        selected.append(pool.pop(best_idx))
    return selected


# ===== Query 처리 (Phase 3: Intent + Sub-query + Multi-paragraph HyDE) =====

QUERY_ANALYSIS_PROMPT = """사용자의 한국어 인도네시아 법령 질문을 분석해서 retrieval에 활용할 5가지 정보를 추출하세요.

질문: {question}

다음을 JSON으로만 출력 (다른 설명 없이):
{{
  "intent": "single_answer | article_lookup | hierarchy | comparison | case_application | general",
  "sub_queries": [],
  "id_keywords": [],
  "hypothetical_id_answers": [],
  "category_filter": []
}}

각 필드 의미:
1. **intent**: 질문 유형 (정확히 6가지 중 하나)
   - single_answer: 단답형 (예: "X의 공식 명칭은?")
   - article_lookup: 조항 본문 조회 (예: "Pasal 25 내용")
   - hierarchy: 법령 위계/관계 (예: "법령 체계 위계")
   - comparison: 두 개 이상 법령/조항 비교
   - case_application: 특정 상황에 어떤 법이 적용?
   - general: 그 외 또는 분야 개요
2. **sub_queries**: 복합 질문이면 2~4개 한국어 sub-question 분해. 단순 질문이면 빈 배열.
3. **id_keywords**: 인도네시아어 핵심 키워드 5~10개. 법령 약칭(UUD, UU, PP, Perpres, Permen, Kepmen, Perda 등), Pasal 번호, ayat, 기관명, 분야 용어. 본문에 그대로 등장할 형태.
4. **hypothetical_id_answers**: 인도네시아 법령 본문 문체로 작성한 가상 답변 2~3 단락 (HyDE). 각 단락은 다른 측면. 인도네시아어로.
5. **category_filter**: 특정 카테고리로 한정 가능하면 다음 중 골라 리스트: heonbeob(헌법), uu(법률), pp(정부령), perpres(대통령령), permen(장관령), kepmen(장관결정), perda(지방조례), lainnya(기타). 무관하면 빈 배열.
"""

# Intent별 검색 strategy
INTENT_STRATEGY = {
    "single_answer":    {"top_k": 8,  "rerank_pool": 30, "fetch_per_query": 20},
    "article_lookup":   {"top_k": 12, "rerank_pool": 40, "fetch_per_query": 30},
    "hierarchy":        {"top_k": 18, "rerank_pool": 60, "fetch_per_query": 30},
    "comparison":       {"top_k": 20, "rerank_pool": 60, "fetch_per_query": 30},
    "case_application": {"top_k": 18, "rerank_pool": 60, "fetch_per_query": 30},
    "general":          {"top_k": 15, "rerank_pool": 50, "fetch_per_query": 30},
}

# Intent별 답변 hint (user_message 끝에 추가, SYSTEM_PROMPT의 query 유형별 strategy 활성화)
INTENT_HINT = {
    "single_answer":    "이 질문은 **단답형**입니다. 핵심 정답을 짧고 정확하게 한 문단으로 답변하세요.",
    "article_lookup":   "이 질문은 **조항 본문 조회**입니다. 원문 인용 → 한국어 번역 → 맥락 설명 순으로 답변하세요.",
    "hierarchy":        "이 질문은 **법령 위계/관계**입니다. 표 또는 다이어그램형 비교 + 각 조항 출처를 명확히 표시하세요.",
    "comparison":       "이 질문은 **비교분석**입니다. 항목별 비교표(주제·적용범위·조항·차이점)와 핵심 결론을 제시하세요.",
    "case_application": "이 질문은 **사례 적용형**입니다. 관련 법령 목록 → 적용 우선순위 → 충돌 시 처리 순으로 답변하세요.",
    "general":          "",
}


def analyze_query(question: str) -> dict:
    """Claude Haiku로 query를 5가지 정보로 분석 (intent + sub_queries + keywords + HyDE + category)."""
    client = get_anthropic()
    default = {
        "intent": "general",
        "sub_queries": [],
        "id_keywords": [],
        "hypothetical_id_answers": [],
        "category_filter": [],
    }
    try:
        resp = client.messages.create(
            model=CLAUDE_HAIKU,
            max_tokens=2000,
            messages=[{"role": "user", "content": QUERY_ANALYSIS_PROMPT.format(question=question)}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return default
        data = json.loads(m.group(0))
        intent = data.get("intent", "general")
        if intent not in INTENT_STRATEGY:
            intent = "general"
        return {
            "intent": intent,
            "sub_queries": data.get("sub_queries", []) or [],
            "id_keywords": data.get("id_keywords", []) or [],
            "hypothetical_id_answers": data.get("hypothetical_id_answers", []) or [],
            "category_filter": data.get("category_filter", []) or [],
        }
    except Exception as exc:
        logger.warning("query analyze 실패, 원본 query로 fallback: %s", exc)
        return default


# 카테고리 alias (analyze가 출력한 코드 → ChromaDB 컬렉션 prefix 매칭)
_CATEGORY_ALIAS = {
    "heonbeob": "constitution",
    "uu": "uu",
    "pp": "pp",
    "perpres": "perpres",
    "permen": "permen",
    "kepmen": "kepmen",
    "perda": "perda",
    "lainnya": "lainnya",
}


# ===== Multi-vector retrieval =====


def multi_query_retrieve(
    question: str, top_k: int, categories: list[str] | None,
) -> tuple[list[dict], dict, dict]:
    """Phase 3: analyze_query 1회 호출 → 다중 query (원본 + sub_queries + keywords + HyDE 단락별) embedding → unique pool 반환.

    Returns:
        (candidates, debug, analysis) — analysis는 intent/strategy 포함 (caller가 답변 생성 시 활용).
    """
    analysis = analyze_query(question)
    intent = analysis["intent"]
    strat = INTENT_STRATEGY[intent]

    # query 리스트 구축 (label, text)
    queries: list[tuple[str, str]] = [("original", question)]
    for i, sq in enumerate(analysis["sub_queries"][:4]):
        if isinstance(sq, str) and sq.strip():
            queries.append((f"sub_{i+1}", sq.strip()))
    if analysis["id_keywords"]:
        queries.append(("id_keywords", " ".join(analysis["id_keywords"])))
    for i, hyde in enumerate(analysis["hypothetical_id_answers"][:3]):
        if isinstance(hyde, str) and hyde.strip():
            queries.append((f"hyde_{i+1}", hyde.strip()))

    embeddings = encode_query([q[1] for q in queries])

    # 컬렉션 선택. analysis.category_filter 또는 caller가 명시한 categories.
    cols = list_v2_collections()
    user_categories = categories
    auto_categories = []
    if not user_categories and analysis["category_filter"]:
        # analyzer가 추천한 카테고리 → ChromaDB 컬렉션명으로 변환
        for c in analysis["category_filter"]:
            mapped = _CATEGORY_ALIAS.get(str(c).lower())
            if mapped:
                auto_categories.append(COLLECTION_PREFIX + mapped)

    if user_categories:
        from index_manager import normalize_category
        wanted = set()
        for c in user_categories:
            cl = c.strip().lower()
            if cl.startswith(COLLECTION_PREFIX):
                wanted.add(cl)
            else:
                wanted.add(COLLECTION_PREFIX + normalize_category(c.strip()).removeprefix("indonesia_"))
        cols = [c for c in cols if c.name in wanted]
    elif auto_categories:
        # auto category는 hard filter가 아니라 강조. 우선 cols 전체 유지하되 가중치 boost는 향후.
        # 현재는 표시만 (debug용)
        pass

    fetch_per = strat["fetch_per_query"]
    candidates: dict[str, dict] = {}
    rrf_scores: dict[str, float] = {}
    col_names_filter = {c.name for c in cols}

    # === Dense retrieval (per-query × per-collection) ===
    for (label, _qtext), emb in zip(queries, embeddings):
        for col in cols:
            col_weight = HIERARCHY_WEIGHTS.get(col.name, DEFAULT_HIERARCHY_WEIGHT)
            if auto_categories and col.name in auto_categories:
                col_weight *= 0.92
            try:
                res = col.query(query_embeddings=[emb], n_results=fetch_per)
            except Exception as exc:
                logger.warning("col %s dense query 실패: %s", col.name, exc)
                continue
            ids = (res.get("ids") or [[]])[0]
            docs = (res.get("documents") or [[]])[0]
            metas = (res.get("metadatas") or [[]])[0]
            dists = (res.get("distances") or [[]])[0]
            for rank, (cid, doc, meta, dist) in enumerate(zip(ids, docs, metas, dists)):
                try:
                    page_num = int(meta.get("page", 99) or 99)
                except (ValueError, TypeError):
                    page_num = 99
                page_pen = EARLY_PAGE_PENALTY if page_num <= EARLY_PAGE_THRESHOLD else 1.0
                eff = (dist if dist is not None else 1.0) * col_weight * page_pen
                # RRF: 1/(k+rank+1), col_weight로 나눠 위계 선호 유지
                rrf_scores[cid] = rrf_scores.get(cid, 0.0) + (1.0 / (RRF_K + rank + 1)) / col_weight
                prev = candidates.get(cid)
                if prev is None or eff < prev["eff_dist"]:
                    candidates[cid] = {
                        "id": cid,
                        "text": doc,
                        "metadata": meta,
                        "dist": dist,
                        "eff_dist": eff,
                        "source_query": label,
                        "collection": col.name,
                    }

    # === BM25 retrieval (global, original + id_keywords만 — HyDE/sub는 dense에 맡김) ===
    bm25_queries: list[tuple[str, str]] = [("original_bm25", question)]
    if analysis["id_keywords"]:
        bm25_queries.append(("id_keywords_bm25", " ".join(analysis["id_keywords"])))
    bm25_used = False
    bm25_top_n = fetch_per * BM25_FETCH_MULTIPLIER
    for label, qtext in bm25_queries:
        try:
            hits = bm25_search_global(qtext, top_n=bm25_top_n)
        except Exception as exc:
            logger.warning("BM25 검색 실패 (%s): %s", label, exc)
            continue
        if hits:
            bm25_used = True
        for cid, text, meta, col_name, rank in hits:
            # 사용자가 카테고리를 한정했다면 그 컬렉션만 통과시킴
            if col_name not in col_names_filter:
                continue
            col_weight = HIERARCHY_WEIGHTS.get(col_name, DEFAULT_HIERARCHY_WEIGHT)
            if auto_categories and col_name in auto_categories:
                col_weight *= 0.92
            rrf_scores[cid] = rrf_scores.get(cid, 0.0) + (1.0 / (RRF_K + rank + 1)) / col_weight
            if cid not in candidates:
                try:
                    page_num = int((meta or {}).get("page", 99) or 99)
                except (ValueError, TypeError):
                    page_num = 99
                page_pen = EARLY_PAGE_PENALTY if page_num <= EARLY_PAGE_THRESHOLD else 1.0
                candidates[cid] = {
                    "id": cid,
                    "text": text,
                    "metadata": meta,
                    "dist": None,
                    "eff_dist": col_weight * page_pen,
                    "source_query": label,
                    "collection": col_name,
                }

    # RRF score 부여 후 내림차순 정렬
    for cid, c in candidates.items():
        c["rrf_score"] = rrf_scores.get(cid, 0.0)
    ranked = sorted(candidates.values(), key=lambda c: -c["rrf_score"])

    # Hierarchical filter — 일부 intent에서 top-N source로 한정
    ranked, hier_info = _hierarchical_filter(ranked, intent)

    # MMR — 같은 source/Pasal 중복 다양화
    cand_list = _mmr_select(ranked, target_size=strat["rerank_pool"], lambda_=MMR_LAMBDA)

    debug = {
        "analysis": {
            "intent": intent,
            "sub_queries": analysis["sub_queries"],
            "id_keywords": analysis["id_keywords"],
            "hypothetical_id_answers_count": len(analysis["hypothetical_id_answers"]),
            "category_filter": analysis["category_filter"],
        },
        "strategy": strat,
        "num_queries": len(queries),
        "num_bm25_queries": len(bm25_queries),
        "bm25_used": bm25_used,
        "candidates_unique": len(candidates),
        "candidates_topN": len(cand_list),
        "auto_categories": auto_categories,
        "mmr_lambda": MMR_LAMBDA,
        "hierarchical": hier_info,
    }
    return cand_list, debug, analysis


# ===== LLM-as-reranker =====

# ===== Phase 6: Agentic Tool Use =====
# Claude가 답변 도중 추가 정보가 필요하면 직접 도구를 호출.
# 세 가지 핵심 도구:
#   1) search_collection — 특정 카테고리에서 추가 검색
#   2) fetch_article_chunks — 특정 PDF + 조항 직접 조회
#   3) fetch_cross_reference — 본문 인용 문구 자동 파싱 → 해당 법령 조항 조회

TOOLS = [
    {
        "name": "search_collection",
        "description": (
            "특정 인도네시아 법령 카테고리에서 추가 검색. "
            "답변 작성 중 더 많은 정보가 필요하면 호출. "
            "카테고리: constitution(헌법), uu(법률), pp(정부령), perpres(대통령령), "
            "permen(장관령), kepmen(장관결정), perda(지방조례), lainnya(기타)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": ["constitution", "uu", "pp", "perpres", "permen", "kepmen", "perda", "lainnya"],
                },
                "query": {"type": "string", "description": "인도네시아어 또는 한국어 검색 키워드"},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 15, "default": 5},
            },
            "required": ["category", "query"],
        },
    },
    {
        "name": "fetch_article_chunks",
        "description": (
            "특정 PDF 파일과 조항 번호로 청크 직접 조회. "
            "사용자가 명시적 조항을 묻거나 더 자세한 본문이 필요할 때 사용. "
            "pasal 인자를 비우면 PDF 전체 청크 (최대 20개) 반환."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source_file": {
                    "type": "string",
                    "description": "PDF 파일명 (예: UU_12_Tahun_2011_Pembentukan_Peraturan_Perundang-undangan.pdf)",
                },
                "pasal": {
                    "type": "string",
                    "description": "조항 번호 (예: 'Pasal 7'). 비우면 파일 전체.",
                },
            },
            "required": ["source_file"],
        },
    },
    {
        "name": "fetch_cross_reference",
        "description": (
            "본문 내 참조 문구를 자동 파싱해 해당 법령의 조항 청크를 가져옴. "
            "예: 'Pasal 5 ayat (2) UU Nomor 11 Tahun 2020', 'PP 5/2021 Pasal 7'. "
            "법령 종류·번호·연도·조항을 정규식으로 추출 → 카테고리 매핑 → 매칭 PDF 검색 → "
            "fetch_article_chunks 호출. 파일명을 모를 때 cross-citation 따라가는 용도. "
            "파싱/매칭 실패 시 search_collection으로 fallback."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reference": {
                    "type": "string",
                    "description": "원문 그대로의 참조 문구. 예: 'Pasal 5 ayat (2) UU Nomor 11 Tahun 2020'.",
                },
            },
            "required": ["reference"],
        },
        # Phase 8: 마지막 tool에 cache_control 추가 → system + tools 합쳐 ~2400 tokens cache.
        # system 단독(1764)으로는 cache 안 되는데 tools 합치면 작동. 호출당 입력 비용 ~90% ↓.
        "cache_control": {"type": "ephemeral"},
    },
]

MAX_TOOL_ITERATIONS = 5

# ===== Phase 7: Conversational Memory =====
# In-memory conversation store. server restart 시 history 소실 (단순화).
# TTL 1시간 idle, turn 한도 5 — multi-turn follow-up 지원.
CONVERSATION_TTL_SEC = 3600
CONVERSATION_MAX_TURNS = 5
_conversations: dict[str, dict] = {}
_conversations_lock = threading.Lock()


def _gc_conversations() -> None:
    """만료된 대화 정리 (lock 안에서 호출)."""
    now = time.time()
    stale = [k for k, v in _conversations.items() if now - v.get("last_ts", 0) > CONVERSATION_TTL_SEC]
    for k in stale:
        _conversations.pop(k, None)


def get_or_create_conversation(conv_id: str | None) -> tuple[str, list]:
    """conv_id 없거나 만료면 새로 생성. (new_id, history_messages) 반환."""
    with _conversations_lock:
        _gc_conversations()
        if conv_id and conv_id in _conversations:
            conv = _conversations[conv_id]
            conv["last_ts"] = time.time()
            return conv_id, list(conv["messages"])
        new_id = conv_id or uuid.uuid4().hex[:16]
        _conversations[new_id] = {
            "messages": [],
            "last_ts": time.time(),
            "created": time.time(),
        }
        return new_id, []


def update_conversation(conv_id: str, user_question: str, assistant_text: str) -> None:
    """대화 turn 추가. tool_use 중간 turns은 저장하지 않고 final answer text만 보존."""
    if not assistant_text.strip():
        return
    with _conversations_lock:
        conv = _conversations.get(conv_id)
        if not conv:
            return
        conv["messages"].append({"role": "user", "content": user_question})
        conv["messages"].append({"role": "assistant", "content": assistant_text})
        conv["last_ts"] = time.time()
        # turn 한도 적용 (user+assistant = 2 message). 오래된 turn 제거.
        max_msgs = CONVERSATION_MAX_TURNS * 2
        while len(conv["messages"]) > max_msgs:
            conv["messages"].pop(0)
            if conv["messages"] and conv["messages"][0].get("role") == "assistant":
                conv["messages"].pop(0)
TOOL_RESULT_MAX_CHUNKS = 8       # 단일 tool 결과당 LLM에 줄 청크 수 한도
TOOL_RESULT_TEXT_MAX = 800       # 각 청크 텍스트 truncate

_CATEGORY_TO_COLLECTION = {
    "constitution": "v2_indonesia_constitution",
    "heonbeob": "v2_indonesia_constitution",
    "uu": "v2_indonesia_uu",
    "pp": "v2_indonesia_pp",
    "perpres": "v2_indonesia_perpres",
    "permen": "v2_indonesia_permen",
    "kepmen": "v2_indonesia_kepmen",
    "perda": "v2_indonesia_perda",
    "lainnya": "v2_indonesia_lainnya",
}


def tool_search_collection(category: str, query: str, top_k: int = 5) -> dict:
    cat = (category or "").strip().lower()
    col_name = _CATEGORY_TO_COLLECTION.get(cat)
    if not col_name:
        return {"error": f"unknown category: {category}", "results": []}
    client = get_chroma()
    try:
        col = client.get_collection(col_name)
    except Exception as exc:
        return {"error": f"collection not found: {exc}", "results": []}
    emb = encode_query([query])[0]
    n = max(1, min(int(top_k or 5), TOOL_RESULT_MAX_CHUNKS))
    try:
        res = col.query(query_embeddings=[emb], n_results=n)
    except Exception as exc:
        return {"error": f"chroma query failed: {exc}", "results": []}
    docs = (res.get("documents") or [[]])[0]
    metas = (res.get("metadatas") or [[]])[0]
    dists = (res.get("distances") or [[]])[0]
    results = []
    for doc, meta, dist in zip(docs, metas, dists):
        results.append({
            "source": str(meta.get("source", "")),
            "page": meta.get("page", ""),
            "article": str(meta.get("article", "")),
            "score": round(1.0 - (dist if dist is not None else 1.0), 3),
            "text": clean_chunk(doc)[:TOOL_RESULT_TEXT_MAX],
        })
    return {"category": cat, "collection": col_name, "count": len(results), "results": results}


# Phase 9: source_file prefix → category 매핑 (모든 컬렉션 scan 22s → 단일 컬렉션 0.3s)
_SOURCE_PREFIX_MAP = [
    ("UUD_", "constitution"),
    ("UU_7-1950_UUDS", "constitution"),  # 임시헌법
    ("UU_11-1949_Konstitusi", "constitution"),  # RIS 헌법
    ("UU_", "uu"),
    ("PP_", "pp"),
    ("Perpres_", "perpres"),
    ("Permen", "permen"),     # Permen, Permenesdm, Permenkumham, Permenkes 등 모두
    ("Kepmen", "kepmen"),     # Kepmen, Kepmenesdm 등
    ("Perda_", "perda"),
    ("Inpres_", "lainnya"),
]


def _category_from_source(source_file: str) -> str | None:
    """파일명 prefix로 카테고리 추정. 매칭 실패 시 None (fallback to full scan)."""
    if not source_file:
        return None
    for prefix, cat in _SOURCE_PREFIX_MAP:
        if source_file.startswith(prefix):
            return cat
    return None


def tool_fetch_article_chunks(source_file: str, pasal: str | None = None) -> dict:
    client = get_chroma()
    results = []

    # Phase 9: source_file로 카테고리 추정해 단일 컬렉션만 scan
    category = _category_from_source(source_file)
    if category:
        target_cols = [COLLECTION_PREFIX + category]
    else:
        # 추정 실패 시 fallback: 모든 v2 컬렉션 scan
        target_cols = [c.name for c in client.list_collections() if c.name.startswith(COLLECTION_PREFIX)]

    for col_name in target_cols:
        try:
            col = client.get_collection(col_name)
        except Exception:
            continue
        try:
            # Phase 9 fix: source 단독 where (0.3s). article 필터는 Python에서.
            # ChromaDB의 $and 필터가 대량 컬렉션에서 매우 느림 (50s+).
            res = col.get(where={"source": source_file}, limit=200)
        except Exception:
            continue
        docs = res.get("documents") or []
        metas = res.get("metadatas") or []
        ids = res.get("ids") or []
        for doc, meta, cid in zip(docs, metas, ids):
            # Python 메모리 필터 (matching ms)
            if pasal:
                art = str(meta.get("article", ""))
                if not art:
                    continue
                # exact 또는 prefix 매칭 (Pasal 7과 Pasal 7A 둘 다 허용)
                if art != pasal and not art.startswith(pasal):
                    continue
            results.append({
                "id": cid,
                "source": str(meta.get("source", "")),
                "page": meta.get("page", ""),
                "article": str(meta.get("article", "")),
                "text": clean_chunk(doc)[:TOOL_RESULT_TEXT_MAX],
            })
            if len(results) >= TOOL_RESULT_MAX_CHUNKS:
                break
        if len(results) >= TOOL_RESULT_MAX_CHUNKS:
            break
    return {
        "source_file": source_file,
        "pasal": pasal or "",
        "category_resolved": category or "fallback_scan",
        "count": len(results),
        "results": results,
    }


_CROSSREF_TYPE_RE = re.compile(
    r"\b(UUD|UUDS|UURIS|UU|PP|Perpres|Permen[a-zA-Z]*|Kepmen[a-zA-Z]*|Perda|Inpres|Konstitusi)\b",
    re.IGNORECASE,
)
_CROSSREF_NUMYEAR_RE = re.compile(
    r"(?:No\.?\s*|Nomor\s+)?(\d+)\s*(?:Tahun\s+|/|-)\s*(\d{4})",
    re.IGNORECASE,
)
_CROSSREF_PASAL_RE = re.compile(
    r"Pasal\s+(\d+[A-Za-z]?)",
    re.IGNORECASE,
)


def _crossref_type_to_category(ltype: str) -> str | None:
    t = (ltype or "").lower()
    if t in ("uud", "uuds", "uuris", "konstitusi"):
        return "constitution"
    if t == "uu":
        return "uu"
    if t == "pp":
        return "pp"
    if t == "perpres":
        return "perpres"
    if t.startswith("permen"):
        return "permen"
    if t.startswith("kepmen"):
        return "kepmen"
    if t == "perda":
        return "perda"
    if t == "inpres":
        return "lainnya"
    return None


def _list_unique_sources(col) -> list[str]:
    """컬렉션의 unique source 목록. BM25 인덱스가 있으면 거기서, 없으면 col.get."""
    idx = _bm25_state.get("global")
    if idx and idx.get("collections"):
        seen: set[str] = set()
        for cn, m in zip(idx["collections"], idx["metas"]):
            if cn != col.name:
                continue
            s = (m or {}).get("source")
            if s:
                seen.add(s)
        if seen:
            return sorted(seen)
    try:
        r = col.get(include=["metadatas"])
    except Exception:
        return []
    seen2: set[str] = set()
    for m in (r.get("metadatas") or []):
        s = (m or {}).get("source")
        if s:
            seen2.add(s)
    return sorted(seen2)


def tool_fetch_cross_reference(reference: str) -> dict:
    """본문 참조 문구 → 파싱 → 매칭 PDF → fetch_article_chunks."""
    ref = (reference or "").strip()
    if not ref:
        return {"error": "empty reference", "reference": ref, "results": []}
    type_m = _CROSSREF_TYPE_RE.search(ref)
    num_m = _CROSSREF_NUMYEAR_RE.search(ref)
    pasal_m = _CROSSREF_PASAL_RE.search(ref)
    if not type_m:
        return {"error": "parse failed: 법령 종류 인식 불가", "reference": ref, "results": []}
    ltype = type_m.group(1)
    cat = _crossref_type_to_category(ltype)
    if not cat:
        return {"error": f"unknown law type: {ltype}", "reference": ref, "results": []}
    is_constitution = cat == "constitution"
    # UUD/UUDS/Konstitusi는 Nomor 없이 연도만 있는 경우 허용. 그 외는 Nomor+연도 필수.
    if not is_constitution and not num_m:
        return {
            "error": "parse failed: 법령 번호·연도 인식 불가",
            "reference": ref,
            "results": [],
        }
    num = num_m.group(1) if num_m else None
    year = num_m.group(2) if num_m else None
    if is_constitution and year is None:
        ym = re.search(r"\b(19\d{2}|20\d{2})\b", ref)
        if ym:
            year = ym.group(1)
    pasal = f"Pasal {pasal_m.group(1)}" if pasal_m else None
    col_name = COLLECTION_PREFIX + cat
    client = get_chroma()
    try:
        col = client.get_collection(col_name)
    except Exception as exc:
        return {"error": f"collection not found: {exc}", "reference": ref, "results": []}
    sources = _list_unique_sources(col)
    if not sources:
        return {"error": "collection empty", "reference": ref, "results": []}

    type_token = re.escape(ltype)
    matched: list[str] = []
    if is_constitution:
        # UUD_1945.pdf, UUD_NRI_Tahun_1945.pdf 등 다양한 파일명을 허용.
        if year:
            uud_re = re.compile(rf"UUD.*{re.escape(year)}", re.IGNORECASE)
            matched = [s for s in sources if uud_re.search(s)]
        if not matched:
            matched = [s for s in sources if re.search(r"^UUD", s, re.IGNORECASE)]
    else:
        # {TYPE}_{NUM}_Tahun_{YEAR}_* (가장 흔한 파일명 규약)
        match_re = re.compile(
            rf"^{type_token}_0*{re.escape(num)}_Tahun_{re.escape(year)}",
            re.IGNORECASE,
        )
        matched = [s for s in sources if match_re.search(s)]
        if not matched:
            # 느슨한 fallback: _{NUM}_Tahun_{YEAR}_ 어디든
            alt_re = re.compile(
                rf"_0*{re.escape(num)}_Tahun_{re.escape(year)}",
                re.IGNORECASE,
            )
            matched = [s for s in sources if alt_re.search(s)]

    if not matched:
        return {
            "error": "matching source pdf not found",
            "reference": ref,
            "parsed": {"type": ltype, "num": num, "year": year, "pasal": pasal, "category": cat},
            "candidate_sources": len(sources),
            "results": [],
        }
    src = matched[0]
    result = tool_fetch_article_chunks(source_file=src, pasal=pasal)
    result["reference"] = ref
    result["matched_source"] = src
    result["parsed"] = {"type": ltype, "num": num, "year": year, "pasal": pasal, "category": cat}
    return result


def execute_tool(name: str, inp: dict) -> dict:
    try:
        if name == "search_collection":
            return tool_search_collection(
                category=inp.get("category", ""),
                query=inp.get("query", ""),
                top_k=inp.get("top_k", 5),
            )
        elif name == "fetch_article_chunks":
            return tool_fetch_article_chunks(
                source_file=inp.get("source_file", ""),
                pasal=inp.get("pasal"),
            )
        elif name == "fetch_cross_reference":
            return tool_fetch_cross_reference(reference=inp.get("reference", ""))
        else:
            return {"error": f"unknown tool: {name}"}
    except Exception as exc:
        logger.exception("tool execution failed")
        return {"error": f"{type(exc).__name__}: {exc}"}


# ===== Phase 5: Self-Critique + Citation 검증 =====
# 답변 생성 후 Haiku가 답변의 인용/환각/누락을 검증.
# 결과는 SSE 'critique' event 또는 /query 응답의 debug.critique로 노출.

CRITIQUE_PROMPT = """다음은 사용자 질문, 모델이 생성한 답변, 그리고 답변 작성에 사용된 [참고 문서]입니다.
당신은 답변의 사실성·근거성을 검증하는 검수자 역할입니다.

[질문]
{question}

[참고 문서]
{context}

[모델 답변]
{answer}

검증 항목:

1. **citations 확인**: 답변 본문 내 "(Pasal X, 출처: 파일명.pdf, p.NN)" 형식 인용을 추출하고, 각 인용이 [참고 문서]에 실제로 있는지 확인.
2. **hallucination**: 답변에 [참고 문서]에 없는 주장(추측, 외부 지식, 일반 상식)이 있는지 확인. 있다면 어떤 부분인지.
3. **missing**: [참고 문서]에는 있지만 답변이 누락한 중요 정보. 답변 깊이를 높이기 위해 추가했어야 할 인용.
4. **confidence**: 답변 전반의 신뢰도. high(인용 정확, 환각 없음) / medium(부분 미흡) / low(중대한 오류).

반드시 JSON으로만 (다른 설명 없이):
{{
  "confidence": "high",
  "verified_citations_count": 5,
  "issues": [
    {{"type": "hallucination", "description": "..."}},
    {{"type": "bad_citation", "description": "..."}},
    {{"type": "missing", "description": "..."}}
  ],
  "summary": "한국어 한 줄 신뢰도 평가"
}}

issues가 없으면 빈 배열. type은 hallucination/bad_citation/missing 중 하나.
"""


def critique_answer(question: str, context: str, answer: str) -> dict:
    """답변에 대한 self-critique. Haiku 1 call."""
    default = {
        "confidence": "unknown",
        "verified_citations_count": 0,
        "issues": [],
        "summary": "검증 미실행",
    }
    if not answer.strip():
        return default
    client = get_anthropic()
    try:
        # context가 너무 길면 token 비용 ↑. critique용 잘라낸 context 사용.
        # 단 청크 핵심 정보 (출처/조항/본문)은 유지.
        ctx = context[:30000] + ("…[truncated]" if len(context) > 30000 else "")
        ans = answer[:8000]
        resp = client.messages.create(
            model=CLAUDE_HAIKU,
            max_tokens=2000,
            messages=[{"role": "user", "content": CRITIQUE_PROMPT.format(
                question=question, context=ctx, answer=ans,
            )}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return {**default, "summary": "검증 JSON 파싱 실패"}
        data = json.loads(m.group(0))
        # normalize
        return {
            "confidence": data.get("confidence", "unknown"),
            "verified_citations_count": int(data.get("verified_citations_count", 0) or 0),
            "issues": data.get("issues", []) or [],
            "summary": data.get("summary", ""),
        }
    except Exception as exc:
        logger.warning("critique 실패: %s", exc)
        return {**default, "summary": f"검증 호출 실패: {type(exc).__name__}"}


# ===== Phase 11: Adversarial critique (다른 모델군 cross-check) =====
# Haiku self-critique 결과가 low/medium+issues면 Sonnet으로 한 번 더 검증.
# 같은 모델군 self-critique가 놓치는 환각·인용 오류를 적발.

ADVERSARIAL_CRITIQUE_PROMPT = """당신은 인도네시아 법령 답변의 **adversarial reviewer**입니다.
다른 검수자(Haiku)가 이미 1차 검증을 했고, 그 결과를 아래에 첨부합니다. 당신의 역할은 1차 검증이 **놓쳤을 가능성**을 찾는 것 — 같은 모델군의 self-critique는 비슷한 종류의 오류를 함께 놓치는 경향이 있습니다.

[질문]
{question}

[참고 문서]
{context}

[모델 답변]
{answer}

[1차 검증 결과 — Haiku]
- confidence: {initial_confidence}
- summary: {initial_summary}
- issues: {initial_issues}

검증 항목 (1차 결과를 의심하며):

1. **놓친 hallucination**: 1차에서 verified로 분류됐지만 사실 [참고 문서]에 근거가 약하거나 외삽된 부분.
2. **subtle bad_citation**: 인용 형식은 맞지만 실제로는 다른 조항/페이지를 가리키는 경우. ayat 번호 미스매치, 같은 PDF의 인접 조항 혼동.
3. **missing**: 답변이 핵심 조항·예외·반대 사례를 누락. 특히 위계 충돌 시 상위법 명시 누락.
4. **logical_gap**: 인용은 정확하나 결론이 인용에서 직접 도출되지 않는 비약.
5. **confidence 재평가**: high/medium/low 중 어디로 조정해야 하는지.

반드시 JSON으로만:
{{
  "confidence": "high|medium|low",
  "additional_issues": [
    {{"type": "hallucination|bad_citation|missing|logical_gap", "description": "..."}}
  ],
  "summary": "한국어 한 줄 — 1차 결과와 비교한 종합 평가"
}}

1차 검증이 충분히 엄밀했고 추가 발견이 없으면 additional_issues는 빈 배열, confidence는 1차와 같게.
"""


def _critique_needs_adversarial(critique: dict, verifier: dict | None) -> bool:
    """Adversarial critique를 켤지 판단. 비용 통제 위해 의심스러운 답변에만."""
    if not ADV_CRITIQUE_ENABLED:
        return False
    conf = (critique or {}).get("confidence", "")
    issues = (critique or {}).get("issues", []) or []
    if conf == "low":
        return True
    if conf == "medium" and len(issues) >= 1:
        return True
    # verifier가 의심 인용을 표시했으면 high여도 한 번 더.
    if verifier and (verifier.get("unverified") or 0) > 0:
        return True
    return False


def adversarial_critique(question: str, context: str, answer: str, initial: dict) -> dict:
    """Sonnet으로 1차 critique를 challenge. 실패 시 빈 결과 반환 (호출자가 merge skip)."""
    empty = {"confidence": initial.get("confidence", "unknown"), "additional_issues": [], "summary": ""}
    if not answer.strip():
        return empty
    client = get_anthropic()
    try:
        ctx = context[:30000] + ("…[truncated]" if len(context) > 30000 else "")
        ans = answer[:8000]
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": ADVERSARIAL_CRITIQUE_PROMPT.format(
                question=question,
                context=ctx,
                answer=ans,
                initial_confidence=initial.get("confidence", "unknown"),
                initial_summary=initial.get("summary", ""),
                initial_issues=json.dumps(initial.get("issues", []), ensure_ascii=False)[:2000],
            )}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return empty
        data = json.loads(m.group(0))
        return {
            "confidence": data.get("confidence", initial.get("confidence", "unknown")),
            "additional_issues": data.get("additional_issues", []) or [],
            "summary": str(data.get("summary", ""))[:400],
        }
    except Exception as exc:
        logger.warning("adversarial critique 실패: %s", exc)
        return empty


def _merge_critiques(initial: dict, adv: dict) -> dict:
    """1차 + adversarial critique 병합. confidence는 더 낮은 쪽 우선, issues는 합집합."""
    if not adv or (not adv.get("additional_issues") and adv.get("confidence") == initial.get("confidence")):
        return {**initial, "adversarial": {"applied": True, "additional_issues_count": 0, "summary": adv.get("summary", "") if adv else ""}}
    # confidence downgrade rank
    rank = {"high": 3, "medium": 2, "low": 1, "unknown": 0}
    init_r = rank.get(initial.get("confidence", "unknown"), 0)
    adv_r = rank.get(adv.get("confidence", "unknown"), 0)
    final_conf = initial.get("confidence") if init_r and init_r <= adv_r else adv.get("confidence", initial.get("confidence"))
    add_issues = adv.get("additional_issues", []) or []
    merged_issues = list(initial.get("issues", []) or [])
    for ai in add_issues:
        if isinstance(ai, dict):
            merged_issues.append({**ai, "source": "adversarial"})
    return {
        **initial,
        "confidence": final_conf,
        "issues": merged_issues,
        "adversarial": {
            "applied": True,
            "additional_issues_count": len(add_issues),
            "summary": adv.get("summary", ""),
            "confidence_proposed": adv.get("confidence", initial.get("confidence")),
        },
    }


# ===== Phase 5b: critique-driven retry + deterministic citation verifier =====
# critique(LLM)이 hallucination/bad_citation/low confidence를 보고하거나,
# verify_citations(정규식 + chunk metadata 매칭)가 unverified 인용을 찾으면
# 한 번 더 답변 생성한다. 두 번째 답변에는 issue 목록을 명시 제공 → 모델이 문제 부분만 교정.

RETRY_MAX = int(os.getenv("RAG_CRITIQUE_RETRY_MAX", "1"))
RETRY_ON_MEDIUM_MIN_ISSUES = int(os.getenv("RAG_CRITIQUE_RETRY_ON_MEDIUM_MIN_ISSUES", "2"))

# (Pasal 7 ayat (1), 출처: 파일명.pdf, p.5) 패턴. ayat 부분은 옵션, 페이지는 숫자.
# 한국어/영문 컴마, 콜론 변형 허용.
CITATION_RE = re.compile(
    r"\(\s*"
    r"(Pasal\s+[A-Za-z0-9]+(?:\s+ayat\s*\([^)]+\))?)"   # group 1: Pasal X 또는 Pasal X ayat (Y)
    r"\s*[,，]\s*출처\s*[:：]\s*"
    r"([^,，)]+?\.pdf)"                                   # group 2: filename.pdf
    r"\s*[,，]\s*p\.?\s*(\d+)"                            # group 3: page
    r"\s*\)",
    re.IGNORECASE,
)


def verify_citations(answer: str, retrieved: list[dict]) -> dict:
    """답변 내 inline 인용을 retrieved chunks의 metadata와 deterministic 매칭.

    retrieved: rerank/agent loop가 답변 모델에 넘긴 context chunks. 각 항목은
        rag_server_v2 내부 구조로 {"metadata": {"source","page","article",...}, "text":...}.
    """
    by_src: dict[str, list[dict]] = {}
    for c in retrieved:
        m = c.get("metadata") if isinstance(c, dict) else None
        if not m:
            continue
        src = str(m.get("source", "")).strip().lower()
        if src:
            by_src.setdefault(src, []).append(m)

    items: list[dict] = []
    verified = 0
    for match in CITATION_RE.finditer(answer):
        pasal_raw = match.group(1).strip()
        # chunk metadata.article은 "Pasal 7" 형식 (ayat 정보 없음) — ayat 부분 떼고 매칭.
        pasal_key = re.split(r"\s+ayat", pasal_raw, maxsplit=1, flags=re.IGNORECASE)[0].strip()
        src_raw = match.group(2).strip()
        try:
            page_int = int(match.group(3))
        except ValueError:
            page_int = -1
        src_low = src_raw.lower()
        candidates = by_src.get(src_low)
        if not candidates:
            # fuzzy: substring 매칭 (확장자 변형/공백 등 흡수)
            for k, v in by_src.items():
                if src_low in k or k in src_low:
                    candidates = v
                    break
        if not candidates:
            items.append({
                "raw": match.group(0), "pasal": pasal_raw, "source": src_raw, "page": page_int,
                "status": "unverified", "reason": "source_not_in_context",
            })
            continue
        art_ok = any(
            str(m.get("article", "")).strip() == pasal_key
            or str(m.get("article", "")).strip().startswith(pasal_key)
            for m in candidates
        )
        page_ok = any(int(m.get("page", -2) or -2) == page_int for m in candidates)

        # Fallback: chunking 단계에서 article 추출이 부정확한 케이스 (Pasal X 본문이 BAB II 등
        # 다른 article로 marked) 흡수. 같은 source의 retrieved 청크 본문에서 word-boundary로
        # pasal_key를 직접 검색. 본문 내 매치 = verified.
        used_body_fallback = False
        if not art_ok:
            pasal_rx = re.compile(r"\b" + re.escape(pasal_key) + r"\b", re.IGNORECASE)
            for c in retrieved:
                m = c.get("metadata") if isinstance(c, dict) else None
                if not m or str(m.get("source", "")).strip().lower() != src_low:
                    continue
                body = str(c.get("text") or "")
                if pasal_rx.search(body):
                    art_ok = True
                    used_body_fallback = True
                    try:
                        if int(m.get("page", -2) or -2) == page_int:
                            page_ok = True
                    except (ValueError, TypeError):
                        pass
                    break

        if art_ok and page_ok:
            verified += 1
            items.append({
                "raw": match.group(0), "pasal": pasal_raw, "source": src_raw, "page": page_int,
                "status": "verified",
                "reason": "body_fallback" if used_body_fallback else "ok",
            })
        elif art_ok:
            items.append({
                "raw": match.group(0), "pasal": pasal_raw, "source": src_raw, "page": page_int,
                "status": "page_mismatch", "reason": f"article OK, page {page_int} not in retrieved chunks",
            })
        else:
            items.append({
                "raw": match.group(0), "pasal": pasal_raw, "source": src_raw, "page": page_int,
                "status": "article_mismatch", "reason": f"article '{pasal_key}' not in retrieved chunks for {src_raw}",
            })
    return {
        "total": len(items),
        "verified": verified,
        "unverified": len(items) - verified,
        "items": items,
    }


def should_retry(critique: dict, verifier: dict | None = None) -> tuple[bool, str]:
    """retry 필요 여부 + 사유. 사유는 debug 노출용."""
    if RETRY_MAX <= 0:
        return False, "disabled"
    # 1) deterministic verifier — LLM call 없이 즉시 판정. 우선 신호.
    if verifier and (verifier.get("unverified") or 0) > 0:
        return True, f"verifier_unverified={verifier['unverified']}"
    # 2) critique (LLM-based)
    conf = (critique.get("confidence") or "unknown").lower()
    issues = critique.get("issues") or []
    if conf == "low":
        return True, "confidence=low"
    serious = [i for i in issues if (i.get("type") or "") in ("hallucination", "bad_citation")]
    if serious:
        return True, f"serious_issues={len(serious)}"
    if conf == "medium" and len(issues) >= RETRY_ON_MEDIUM_MIN_ISSUES:
        return True, f"medium+issues={len(issues)}"
    return False, "ok"


def build_retry_user_message(
    question: str, context: str, prior_answer: str, critique: dict,
    verifier: dict | None = None,
) -> str:
    """retry 시 모델에게 전달할 user message. 직전 답변과 발견된 문제를 명시."""
    issues = list(critique.get("issues") or [])
    # verifier 검증 실패 항목을 bad_citation issue로 추가 (LLM critique이 놓친 케이스 포함).
    if verifier:
        for it in (verifier.get("items") or []):
            if (it.get("status") or "") == "verified":
                continue
            issues.insert(0, {
                "type": "bad_citation",
                "description": f"인용 '{it.get('raw','')}' 검증 실패 — {it.get('reason','')}",
            })
    issue_lines: list[str] = []
    for i, it in enumerate(issues, start=1):
        t = (it.get("type") or "?").strip()
        desc = (it.get("description") or "").strip()
        issue_lines.append(f"  {i}. [{t}] {desc}")
    issues_txt = "\n".join(issue_lines) if issue_lines else "  - (구체 항목 없음 — 신뢰도 낮음으로 분류)"
    summary = (critique.get("summary") or "").strip()
    return (
        f"[참고 문서]\n{context}\n\n"
        f"[질문]\n{question}\n\n"
        f"[직전 답변]\n{prior_answer}\n\n"
        f"[검수 결과 — 직전 답변에서 발견된 문제]\n"
        f"신뢰도: {critique.get('confidence', 'unknown')}\n"
        f"요약: {summary}\n"
        f"문제 목록:\n{issues_txt}\n\n"
        f"위 [참고 문서]만을 근거로 다음을 지키며 답변을 **처음부터 다시** 작성하세요:\n"
        f"- hallucination/bad_citation으로 지목된 인용은 제거하거나, fetch_article_chunks 도구로 raw 본문을 다시 확인한 뒤에만 인용.\n"
        f"- missing으로 지목된 참고문서 내용은 명시적으로 답변에 포함.\n"
        f"- 모든 조항 번호·페이지·인도네시아어 원문 인용은 [참고 문서]에 실제로 있는 텍스트와 정확히 일치해야 함.\n"
        f"- 직전 답변의 목차/구조를 가능한 유지하되 누락 섹션은 빠짐없이 채울 것.\n"
        f"- 인용 형식: (Pasal X ayat (Y), 출처: 파일명.pdf, p.NN)"
    )


RERANKER_PROMPT = """다음은 사용자의 질문과 검색된 청크들입니다. 각 청크가 질문에 답하는 데 얼마나 유용한지 평가하세요.

[질문]
{question}

[질문 유형]
{intent}

[청크들]
{chunks_block}

각 청크에 0~10 점수 + 짧은 reasoning (1줄)을 매기세요.
- **10점**: 본문 직접 답변, 명확한 조항/원리 명시
- **7~9점**: 본문 관련 정보 풍부 (조항 인용, 정의, 적용 예시)
- **4~6점**: 부분 관련, 보조 정보
- **1~3점**: 거의 무관 (인접 주제)
- **0점**: 서명/표지/페이지 번호/공포일만, 본문 거의 없음

평가 기준:
- 조항 번호(Pasal/BAB)가 명시되어 있고 본문이 풍부할수록 고득점
- 표지/Menimbang/서명란만 있는 청크는 저득점
- 질문 유형에 맞는 정보일수록 가산

reasoning은 한국어로 짧게. 청크에 어떤 내용이 있어서 점수를 줬는지.

반드시 JSON으로만 (다른 설명 없이):
{{"scores": [
  {{"idx": 0, "score": 8, "reason": "Pasal 7 ayat (1) 위계 표 본문"}},
  {{"idx": 1, "score": 3, "reason": "Menimbang 도입부만"}},
  ...
]}}
"""

# Diversity penalty — 같은 PDF 출처가 반복될 때 score 감점.
# 답변 인용이 한 PDF에 몰리는 현상 방지, 다양한 출처에서 인용.
# k-번째 동일 출처 청크의 score multiplier:
#   1st: 1.0 (감점 없음)
#   2nd: 0.85
#   3rd: 0.70
#   4th+: 0.55
DIVERSITY_PENALTY_STEP = 0.15
DIVERSITY_PENALTY_MIN = 0.55


def rerank_with_claude(question: str, candidates: list[dict], top_k: int, intent: str = "general") -> list[dict]:
    """Phase 4 + Phase 11: 선택적 cross-encoder 1차 컷 → Claude Haiku 2차 reranking + reasoning.

    각 candidate에 'llm_score', 'llm_reason'을 추가. caller가 답변 단계에서 reasoning을 활용.
    cross-encoder가 활성화되어 있으면 Haiku 호출 전에 후보를 CROSS_ENCODER_CUT개로 줄여 비용·지연을 절감.
    """
    # Phase 11: cross-encoder 1차 컷 — 큰 pool을 신속하게 솎아냄.
    if CROSS_ENCODER_ENABLED and len(candidates) > CROSS_ENCODER_CUT:
        candidates = cross_encoder_score(question, candidates, cut_to=max(CROSS_ENCODER_CUT, top_k))

    if len(candidates) <= top_k:
        # 그래도 reasoning 없이 그대로 반환
        for c in candidates:
            c.setdefault("llm_score", 5.0)
            c.setdefault("llm_reason", "")
        return candidates

    # 청크 텍스트 800자까지 — 더 정확한 평가 위해
    def short(s: str, n: int = 800) -> str:
        s = clean_chunk(s)
        return s[:n] + ("…" if len(s) > n else "")

    chunks_block = "\n\n".join(
        f"[{i}] (출처={c['metadata'].get('source')}, p{c['metadata'].get('page')}, "
        f"{c['metadata'].get('article','')}, {c['collection'].replace('v2_indonesia_','')})\n{short(c['text'])}"
        for i, c in enumerate(candidates)
    )

    client = get_anthropic()
    try:
        resp = client.messages.create(
            model=CLAUDE_HAIKU,
            max_tokens=3000,
            messages=[{"role": "user", "content": RERANKER_PROMPT.format(
                question=question, intent=intent, chunks_block=chunks_block,
            )}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            logger.warning("reranker 응답 파싱 실패, dense 순서 그대로 사용")
            for c in candidates[:top_k]:
                c["llm_score"] = 5.0
                c["llm_reason"] = ""
            return candidates[:top_k]
        data = json.loads(m.group(0))
        scores_list = data.get("scores", [])
        score_map: dict[int, tuple[float, str]] = {}
        for s in scores_list:
            try:
                idx = int(s["idx"])
                sc = float(s["score"])
                reason = str(s.get("reason", ""))[:200]
                score_map[idx] = (sc, reason)
            except (KeyError, ValueError, TypeError):
                continue

        # 각 candidate에 LLM 점수/이유 부착
        for i, c in enumerate(candidates):
            sc, reason = score_map.get(i, (5.0, ""))
            c["llm_score"] = sc
            c["llm_reason"] = reason

        # Diversity penalty: 같은 source의 k번째 청크는 score multiplier 적용
        # 우선 llm_score 내림차순 + eff_dist tiebreak
        sorted_cands = sorted(
            candidates,
            key=lambda c: (-c["llm_score"], c["eff_dist"]),
        )
        source_seen: dict[str, int] = {}
        final = []
        for c in sorted_cands:
            src = str(c["metadata"].get("source", ""))
            k = source_seen.get(src, 0)
            mul = max(DIVERSITY_PENALTY_MIN, 1.0 - DIVERSITY_PENALTY_STEP * k)
            c["llm_score_adjusted"] = c["llm_score"] * mul
            c["llm_score_mul"] = mul
            source_seen[src] = k + 1
            final.append(c)
        # diversity-adjusted 점수로 재정렬
        final.sort(key=lambda c: (-c["llm_score_adjusted"], c["eff_dist"]))
        return final[:top_k]
    except Exception as exc:
        logger.warning("reranker 실패, fallback: %s", exc)
        for c in candidates[:top_k]:
            c.setdefault("llm_score", 5.0)
            c.setdefault("llm_reason", "")
        return candidates[:top_k]


# ===== 답변 생성 =====

# Prompt caching: system prompt를 ephemeral cache block으로 등록 → 같은 prompt 재호출 시
# 입력 토큰 90% 비용 절감 + latency 30~50% 감소.
# Anthropic은 system을 list 형태로 받을 때 각 block에 cache_control 지정 가능.
SYSTEM_PROMPT = """당신은 인도네시아 법령 전문가입니다. 주어진 [참고 문서]만을 근거로 사용자 질문에 **한국어로** 깊이 있게 답변하세요. 추측하지 말고 문서에 명시된 사실만 사용합니다.

## 1. 인도네시아 법령 위계 체계

법령은 다음 위계로 정렬됩니다 (UU 12/2011 Pasal 7 ayat 1):

| 순위 | 법령 종류 | 한국어 표기 | 약칭 |
|------|----------|------------|------|
| 1 | Undang-Undang Dasar Negara Republik Indonesia Tahun 1945 | 1945년 헌법 | UUD |
| 2 | Ketetapan Majelis Permusyawaratan Rakyat | 국민협의회 결정 | TAP MPR |
| 3 | Undang-Undang / Perppu | 법률 / 법률대체 정부령 | UU / Perppu |
| 4 | Peraturan Pemerintah | 정부령 | PP |
| 5 | Peraturan Presiden | 대통령령 | Perpres |
| 6 | Peraturan Daerah Provinsi | 주(州) 지방조례 | Perda Provinsi |
| 7 | Peraturan Daerah Kabupaten/Kota | 군·시 지방조례 | Perda Kab/Kota |

추가: Permen(장관령), Kepmen(장관결정), Inpres(대통령지시) 등은 Pasal 8 ayat 1 별도 범주이며 상위법의 위임 또는 고유 권한이 있을 때만 효력.

## 2. 답변 원칙

1. **언어**: 본문은 반드시 **한국어**로 작성. 단 법령 원문 인용은 **인도네시아어 원문 그대로** 큰따옴표 안에 인용 (번역만 따로 표기).
2. **근거 제시**: 모든 핵심 주장에 출처를 본문 내에 `(Pasal X ayat (Y), 출처: 파일명.pdf, p.NN)` 형식으로 inline 표기.
3. **위계 충돌**: 같은 사안에 다른 위계 법령이 충돌하면 상위법 우선이라는 사실을 **명시**하고, 두 조항을 모두 인용한 뒤 결론.
4. **근거 부족**: 참고 문서에 명확한 근거가 없으면 *"제공된 문서에서 해당 내용을 찾을 수 없습니다"* 라고 답변. 비슷한 조항이 있으면 그것을 명시한 뒤 한계를 알림.
5. **추측 금지**: 일반 상식, 외부 지식, "보통은…" 같은 표현 금지. 오직 [참고 문서] 텍스트만 근거.
6. **구조화**: 제목(##), 목록, 표를 적극 활용. 단편 문장 나열이 아닌 종합적·체계적 답변 선호.

## 3. 인용 형식 예시

**올바른 예시:**

> UU 12/2011 Pasal 7 ayat (1)에 따르면 법령 위계는 UUD 1945를 최상위로 합니다.
> *"Jenis dan hierarki Peraturan Perundang-undangan terdiri atas..."* (Pasal 7 ayat (1), 출처: UU_12_Tahun_2011_Pembentukan_Peraturan_Perundang-undangan.pdf, p.1)

**잘못된 예시 (피해야 할):**

> 보통 인도네시아에서는 헌법이 가장 위입니다. (← 출처 없음, 추측)
> 위계는 1945년 헌법이 최상위입니다. (← 인용 형식 안 맞음)

## 4. Query 유형별 응답 전략

- **단답형** ("X 법령의 공식 명칭은?"): 핵심 정답을 한 문단으로 + 출처 1~2개.
- **조항 조회형** ("Pasal 25 내용은?"): 원문 인용 → 한국어 번역 → 맥락 설명.
- **위계/관계형** ("X와 Y의 관계"): 표 또는 다이어그램형 비교 + 각 조항 출처.
- **분석/비교형** ("A 법령과 B 법령의 차이"): 항목별 비교표(주제, 적용범위, 조항, 차이점) → 핵심 결론.
- **사례 적용형** ("X 상황에서 어떤 법이?"): 관련 법령 목록 → 적용 우선순위 → 위계 충돌 시 처리.

## 5. 답변 끝 "출처:" 섹션

답변 마지막에 반드시 `## 출처:` 섹션을 두고 사용한 모든 인용을 표로 정리:

| 법령 종류 | 파일명 | 페이지 | 조항 |
|----------|--------|--------|------|
| UU | UU_12_Tahun_2011.pdf | p.1 | Pasal 7 ayat (1)(2) |

## 6. 답변 길이

질문 복잡도에 비례. 단답형은 짧게(~200자), 분석/비교형은 충실히(2000자+). "충실함 우선" — 단편적 답변보다 누락 없는 종합 답변 선호.
"""

# Anthropic prompt caching용 block. 매 호출마다 동일한 system prompt를
# ephemeral cache (~5분 TTL)로 등록 → 입력 토큰 비용 ~90% ↓, latency 30~50% ↓.
# 첫 호출은 cache_creation_input_tokens 과금, 이후 호출은 cache_read_input_tokens (10% 가격).
SYSTEM_PROMPT_CACHED = [
    {
        "type": "text",
        "text": SYSTEM_PROMPT,
        "cache_control": {"type": "ephemeral"},
    }
]


# ===== API 모델 =====


class QueryRequest(BaseModel):
    question: str = Field(..., min_length=1)
    top_k: int = Field(TOP_K_DEFAULT, ge=1, le=20)
    categories: list[str] | None = Field(default=None)
    # Phase 7: multi-turn 대화. None이면 새 conversation 시작, str이면 이어가기.
    conversation_id: str | None = Field(default=None)


class SourceItem(BaseModel):
    source: str
    page: int
    article: str
    category: str
    score: float
    snippet: str


class QueryResponse(BaseModel):
    answer: str
    sources: list[SourceItem]
    debug: dict | None = None


# ===== Health/Lifecycle =====


def _warmup_sync() -> None:
    _state["warmup_started"] = True
    started = time.perf_counter()
    try:
        logger.info("v2 warmup: BGE-M3 모델 로딩")
        m = get_embed_model()
        m.encode(["warmup"], batch_size=1, normalize_embeddings=True, convert_to_numpy=True)

        logger.info("v2 warmup: ChromaDB 연결 검증")
        cols = list_v2_collections()
        for c in cols:
            try:
                c.peek(limit=1)
            except Exception:
                pass

        _state["ready"] = True
        _state["readiness_error"] = None
        _state["warmup_finished_ts"] = time.time()
        logger.info("v2 warmup 완료 (%.1fs)", time.perf_counter() - started)
        # BM25 인덱스는 ready=True 이후 백그라운드로 마저 채움.
        # 디스크 캐시가 살아있으면 수 초, 처음이면 ~30s. 빌드 중에는 dense-only로 응답.
        try:
            idx = get_bm25_global()
            if idx:
                logger.info("BM25 글로벌 인덱스 준비 완료 (n=%d)", idx.get("count", 0))
            else:
                logger.warning("BM25 인덱스 준비 실패 — dense-only fallback")
        except Exception:
            logger.exception("BM25 warmup 실패 — dense-only fallback")
    except Exception as exc:
        _state["ready"] = False
        _state["readiness_error"] = f"{type(exc).__name__}: {exc}"
        _state["warmup_finished_ts"] = time.time()
        logger.exception("v2 warmup 실패")


@app.on_event("startup")
async def _on_startup() -> None:
    import asyncio
    if _state.get("warmup_started"):
        return
    asyncio.create_task(asyncio.to_thread(_warmup_sync))


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"ok": True, "alive": True}


@app.get("/health/live")
def health_live() -> dict[str, Any]:
    return {"ok": True, "alive": True}


@app.get("/health/ready")
def health_ready():
    from fastapi.responses import JSONResponse
    ready = bool(_state.get("ready"))
    body = {
        "ready": ready,
        "version": "v2",
        "embedding_model": EMBEDDING_MODEL,
        "error": _state.get("readiness_error"),
    }
    return JSONResponse(body, status_code=200 if ready else 503)


@app.get("/health")
def health(quick: int = 0) -> dict[str, Any]:
    cached = _state.get("health_cache")
    cached_ts = _state.get("health_cache_ts", 0.0)
    cache_fresh = cached is not None and (time.time() - cached_ts) < HEALTH_CACHE_TTL

    if cache_fresh:
        return cached
    if quick:
        if cached is not None:
            return {**cached, "stale": True}
        return {
            "ok": True, "warming": True, "version": "v2",
            "embedding_model": EMBEDDING_MODEL,
            "anthropic_key_set": bool(ANTHROPIC_API_KEY),
        }

    info: dict[str, Any] = {
        "ok": True, "version": "v2",
        "embedding_model": EMBEDDING_MODEL,
        "chroma_mode": CHROMA_MODE,
        "chroma_target": describe_target(),
        "anthropic_key_set": bool(ANTHROPIC_API_KEY),
    }
    try:
        cols = list_v2_collections()
        per = {c.name: c.count() for c in cols}
        info["collections"] = per
        info["collection_count"] = sum(per.values())
        # 컬렉션별 고유 법령(=source PDF) 수. ingest manifest 기반 — chroma
        # col.get() 전체 metadata fetch는 큰 컬렉션에서 timeout이 잦아 폐기.
        manifest_counts = _load_doc_counts_from_manifest()
        per_docs: dict[str, int] = {c.name: int(manifest_counts.get(c.name, 0)) for c in cols}
        info["collection_docs"] = per_docs
        info["doc_count"] = sum(per_docs.values())
    except Exception as exc:
        info["ok"] = False
        info["error"] = str(exc)

    if info["ok"]:
        _state["health_cache"] = info
        _state["health_cache_ts"] = time.time()
    return info


# ===== /query =====


@app.post("/query", response_model=QueryResponse)
def query(req: QueryRequest, x_api_token: str | None = Header(default=None)) -> QueryResponse:
    require_token(x_api_token)

    t0 = time.time()
    candidates, debug, analysis = multi_query_retrieve(req.question, req.top_k, req.categories)
    t_retrieve = time.time() - t0

    if not candidates:
        return QueryResponse(answer="제공된 문서에서 관련 내용을 찾을 수 없습니다.", sources=[])

    # intent 기반 top_k. 사용자가 명시 top_k 보내도 strategy 최소값 보장.
    top_k_final = max(req.top_k, INTENT_STRATEGY[analysis["intent"]]["top_k"])

    t1 = time.time()
    top = rerank_with_claude(req.question, candidates, top_k_final, intent=analysis["intent"])
    t_rerank = time.time() - t1

    # context build — Phase 4: reranker reasoning을 답변 모델에 힌트로 전달
    blocks = []
    for i, c in enumerate(top, start=1):
        m = c["metadata"]
        article = m.get("article") or "조항 미확인"
        source = m.get("source", "?")
        page = m.get("page", "?")
        cat = m.get("category") or ""
        cleaned_text = clean_chunk(c["text"])
        if len(cleaned_text) > CONTEXT_CHUNK_TEXT_MAX:
            cleaned_text = cleaned_text[:CONTEXT_CHUNK_TEXT_MAX] + "…"
        reason = c.get("llm_reason", "")
        reason_line = f", 관련성: {reason}" if reason else ""
        blocks.append(f"[참고문서 {i}] (출처: {source}, p.{page}, {article}, {cat}{reason_line})\n{cleaned_text}")
    context = "\n\n".join(blocks)

    intent_hint = INTENT_HINT.get(analysis["intent"], "")
    user_message = (
        f"[참고 문서]\n{context}\n\n"
        f"[질문]\n{req.question}\n\n"
        + (f"[질문 유형 안내]\n{intent_hint}\n\n" if intent_hint else "")
        + "위 참고 문서를 근거로 한국어로 충실히 답변해 주세요. "
        "인용한 조항/페이지/파일명을 본문과 마지막 '출처:' 섹션에 모두 표시하세요."
    )

    t2 = time.time()
    client = get_anthropic()
    resp = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=MAX_ANSWER_TOKENS,
        thinking={"type": "enabled", "budget_tokens": THINKING_BUDGET_TOKENS},
        system=SYSTEM_PROMPT_CACHED,
        messages=[{"role": "user", "content": user_message}],
    )
    # thinking 블록은 별도 (type="thinking"). 사용자에게 보이는 답변은 text 블록만.
    answer_parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
    answer = "\n".join(answer_parts).strip() or "(빈 응답)"
    t_generate = time.time() - t2
    # cache usage 통계 (디버그)
    usage = resp.usage
    cache_stats = {
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
        "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", None),
        "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", None),
    }

    # Phase 5: self-critique + deterministic citation verifier
    t3 = time.time()
    critique = critique_answer(req.question, context, answer)
    verifier = verify_citations(answer, top)
    # Phase 11: 의심스러운 답변에 한해 Sonnet adversarial critique 추가
    if _critique_needs_adversarial(critique, verifier):
        adv = adversarial_critique(req.question, context, answer, critique)
        critique = _merge_critiques(critique, adv)
    t_critique = time.time() - t3

    # Phase 5b: critique/verifier-driven retry.
    retry_reason = "ok"
    retry_count = 0
    t_retry = 0.0
    do_retry, retry_reason = should_retry(critique, verifier)
    if do_retry:
        retry_count = 1
        retry_msg = build_retry_user_message(req.question, context, answer, critique, verifier)
        t_retry_start = time.time()
        try:
            resp2 = client.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=MAX_ANSWER_TOKENS,
                thinking={"type": "enabled", "budget_tokens": THINKING_BUDGET_TOKENS},
                system=SYSTEM_PROMPT_CACHED,
                messages=[{"role": "user", "content": retry_msg}],
            )
            answer2 = "\n".join(
                b.text for b in resp2.content if getattr(b, "type", None) == "text"
            ).strip()
            if answer2:
                answer = answer2
                u2 = resp2.usage
                for k in ("input_tokens", "output_tokens",
                          "cache_creation_input_tokens", "cache_read_input_tokens"):
                    cache_stats[k] = (cache_stats.get(k) or 0) + (getattr(u2, k, None) or 0)
                # 재검증
                critique = critique_answer(req.question, context, answer)
                verifier = verify_citations(answer, top)
                if _critique_needs_adversarial(critique, verifier):
                    adv = adversarial_critique(req.question, context, answer, critique)
                    critique = _merge_critiques(critique, adv)
        except Exception as exc:
            logger.warning("retry generation 실패: %s", exc)
        t_retry = time.time() - t_retry_start

    sources = []
    for c in top:
        m = c["metadata"]
        sources.append(SourceItem(
            source=str(m.get("source", "?")),
            page=int(m.get("page", 0) or 0),
            article=str(m.get("article") or ""),
            category=str(m.get("category") or ""),
            score=float(1.0 - (c.get("dist") or 1.0)),
            snippet=clean_chunk(c["text"])[:240] + ("…" if len(c["text"]) > 240 else ""),
        ))

    return QueryResponse(
        answer=answer,
        sources=sources,
        debug={
            **debug,
            "selected": len(top),
            "timing": {
                "retrieve_sec": round(t_retrieve, 2),
                "rerank_sec": round(t_rerank, 2),
                "generate_sec": round(t_generate, 2),
                "critique_sec": round(t_critique, 2),
                "retry_sec": round(t_retry, 2),
                "total_sec": round(time.time() - t0, 2),
            },
            "usage": cache_stats,
            "critique": critique,
            "verifier": verifier,
            "retry": {"count": retry_count, "reason": retry_reason},
        },
    )


# ===== /query/stream (SSE) =====
# 답변 토큰을 Server-Sent Events로 progressive 전송.
# 첫 토큰 latency 대폭 ↓ (전체 응답 대기 49s → 첫 토큰 ~1~2s).
# 이벤트 종류:
#   event: sources  → retrieved sources 리스트 (답변 시작 전, 사용자 UI에 출처 카드 먼저)
#   event: token    → 답변 텍스트 chunk
#   event: done     → debug + usage 통계
#   event: error    → 오류
def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.post("/query/stream")
def query_stream(req: QueryRequest, x_api_token: str | None = Header(default=None)):
    require_token(x_api_token)

    t0 = time.time()
    # Phase 7: conversation history 로드 (또는 새 대화 시작)
    conv_id, history_messages = get_or_create_conversation(req.conversation_id)

    def event_gen():
        # conversation_id 먼저 보냄 — frontend가 다음 turn에 이 id 재사용.
        # 이 yield는 multi_query_retrieve보다 먼저 나가야 한다: retrieval은 analyze
        # (Haiku 호출) + 수십 회 ChromaDB 쿼리로 수십 초가 걸리는데, 예전엔 그게
        # event_gen 바깥에서 돌아 첫 SSE 바이트까지 화면이 수십 초 멈춰 보였다.
        # 이제 conversation/retrieving를 즉시 흘려 연결을 살리고 진행 상태를 알린다.
        yield _sse("conversation", {
            "conversation_id": conv_id,
            "turn": len(history_messages) // 2 + 1,
            "is_followup": len(history_messages) > 0,
        })
        yield _sse("retrieving", {"message": "관련 법령 검색 중…"})

        candidates, debug, analysis = multi_query_retrieve(
            req.question, req.top_k, req.categories,
        )
        t_retrieve = time.time() - t0

        if not candidates:
            yield _sse("error", {"message": "제공된 문서에서 관련 내용을 찾을 수 없습니다."})
            return

        top_k_final = max(req.top_k, INTENT_STRATEGY[analysis["intent"]]["top_k"])

        t1 = time.time()
        top = rerank_with_claude(req.question, candidates, top_k_final, intent=analysis["intent"])
        t_rerank = time.time() - t1

        # intent 정보 먼저 SSE로 노출 (UI에 query 유형 뱃지 등 표시 가능)
        yield _sse("intent", {"intent": analysis["intent"], "top_k": top_k_final, "sub_query_count": len(analysis["sub_queries"])})

        # sources — Phase 4: llm_score, llm_reason, diversity mul 포함
        sources_payload = []
        for c in top:
            m = c["metadata"]
            sources_payload.append({
                "source": str(m.get("source", "?")),
                "page": int(m.get("page", 0) or 0),
                "article": str(m.get("article") or ""),
                "category": str(m.get("category") or ""),
                "score": float(1.0 - (c.get("dist") or 1.0)),
                "llm_score": float(c.get("llm_score", 0.0)),
                "llm_reason": str(c.get("llm_reason", "")),
                "diversity_mul": float(c.get("llm_score_mul", 1.0)),
                "snippet": clean_chunk(c["text"])[:240] + ("…" if len(c["text"]) > 240 else ""),
            })
        yield _sse("sources", {"sources": sources_payload})

        # context build — reranker reasoning 포함
        blocks = []
        for i, c in enumerate(top, start=1):
            m = c["metadata"]
            article = m.get("article") or "조항 미확인"
            source = m.get("source", "?")
            page = m.get("page", "?")
            cat = m.get("category") or ""
            cleaned_text = clean_chunk(c["text"])
            if len(cleaned_text) > CONTEXT_CHUNK_TEXT_MAX:
                cleaned_text = cleaned_text[:CONTEXT_CHUNK_TEXT_MAX] + "…"
            reason = c.get("llm_reason", "")
            reason_line = f", 관련성: {reason}" if reason else ""
            blocks.append(f"[참고문서 {i}] (출처: {source}, p.{page}, {article}, {cat}{reason_line})\n{cleaned_text}")
        context = "\n\n".join(blocks)

        intent_hint = INTENT_HINT.get(analysis["intent"], "")
        user_message = (
            f"[참고 문서]\n{context}\n\n"
            f"[질문]\n{req.question}\n\n"
            + (f"[질문 유형 안내]\n{intent_hint}\n\n" if intent_hint else "")
            + "위 참고 문서를 근거로 한국어로 충실히 답변해 주세요. "
            "인용한 조항/페이지/파일명을 본문과 마지막 '출처:' 섹션에 모두 표시하세요."
        )

        # ===== Phase 6+7: Agentic loop + conversation history =====
        t2 = time.time()
        client = get_anthropic()
        cache_stats = {"input_tokens": 0, "output_tokens": 0,
                       "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        accumulated_answer = []
        # Phase 7: 이전 turn history를 messages_loop 앞에 추가
        messages_loop = [*history_messages, {"role": "user", "content": user_message}]
        tool_calls_made = []
        iter_count = 0
        thinking_announced = False  # thinking 시작 이벤트 1회만

        try:
            for iter_n in range(MAX_TOOL_ITERATIONS):
                iter_count = iter_n + 1
                with client.messages.stream(
                    model=CLAUDE_MODEL,
                    system=SYSTEM_PROMPT_CACHED,
                    messages=messages_loop,
                    tools=TOOLS,
                    **_agent_loop_kwargs(),
                ) as stream:
                    cur_block_type = None
                    for event in stream:
                        et = getattr(event, "type", "")
                        if et == "content_block_start":
                            block = getattr(event, "content_block", None)
                            cur_block_type = getattr(block, "type", "") if block else None
                            if cur_block_type == "thinking" and not thinking_announced:
                                thinking_announced = True
                                yield _sse("thinking", {"status": "started"})
                            elif cur_block_type == "text" and thinking_announced:
                                yield _sse("thinking", {"status": "ended"})
                            elif cur_block_type == "tool_use":
                                yield _sse("tool_call_start", {
                                    "name": getattr(block, "name", ""),
                                    "iteration": iter_count,
                                })
                        elif et == "content_block_delta":
                            delta = getattr(event, "delta", None)
                            dt = getattr(delta, "type", "") if delta else ""
                            if dt == "text_delta":
                                accumulated_answer.append(delta.text)
                                yield _sse("token", {"text": delta.text})
                    final_msg = stream.get_final_message()
                    usage = final_msg.usage
                    # 누적 사용량
                    for k in ("input_tokens", "output_tokens",
                              "cache_creation_input_tokens", "cache_read_input_tokens"):
                        v = getattr(usage, k, None) or 0
                        cache_stats[k] = (cache_stats.get(k) or 0) + v

                stop_reason = getattr(final_msg, "stop_reason", None)
                if stop_reason != "tool_use":
                    # end_turn / max_tokens / stop_sequence → 완료
                    break

                # tool_use 실행. assistant message + tool_result 추가 후 다음 iter.
                messages_loop.append({"role": "assistant", "content": final_msg.content})
                tool_results_payload = []
                for block in final_msg.content:
                    if getattr(block, "type", "") != "tool_use":
                        continue
                    tname = block.name
                    tinput = block.input
                    yield _sse("tool_call", {"name": tname, "input": tinput, "iteration": iter_count})
                    t_tool = time.time()
                    result = execute_tool(tname, tinput)
                    elapsed_tool = round(time.time() - t_tool, 2)
                    result_str = json.dumps(result, ensure_ascii=False)
                    tool_calls_made.append({
                        "name": tname, "input": tinput,
                        "result_size": len(result_str), "elapsed_sec": elapsed_tool,
                        "result_count": result.get("count") if isinstance(result, dict) else None,
                    })
                    yield _sse("tool_result", {
                        "name": tname,
                        "result_count": result.get("count") if isinstance(result, dict) else None,
                        "elapsed_sec": elapsed_tool,
                    })
                    tool_results_payload.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_str,
                    })
                messages_loop.append({"role": "user", "content": tool_results_payload})
                # 다음 turn에서 thinking_announced 초기화 — 새 thinking 알림 가능
                thinking_announced = False
            else:
                # for-else: loop가 break 없이 모든 iter 소진 → 마지막 iter도 stop_reason=tool_use.
                # 모델은 더 tool 부르고 싶었지만 한도 도달. tools 제거하고 최종 합성 1회 강제 호출.
                # 이 단계 없으면 사용자에겐 announce 텍스트만 흘러가 "한 줄 답변" 버그 발생.
                logger.info("agent loop hit MAX_TOOL_ITERATIONS (%d), forcing final synthesis without tools", MAX_TOOL_ITERATIONS)
                yield _sse("iter_limit_synthesis", {"message": "조사 단계 마무리 — 최종 답변 합성 중", "iterations": iter_count})
                # 합성용 prompt: 모델에게 더 이상 tool 못 쓰니 가진 자료로 답하라고 명시.
                messages_loop.append({
                    "role": "user",
                    "content": "추가 도구 호출 없이, 지금까지 확보된 [참고 문서]와 tool_result만 근거로 한국어 최종 답변을 작성하세요. 인용 형식(파일명, 페이지, 조항)은 system prompt의 규정 그대로.",
                })
                if thinking_announced:
                    yield _sse("thinking", {"status": "ended"})
                    thinking_announced = False
                with client.messages.stream(
                    model=CLAUDE_MODEL,
                    system=SYSTEM_PROMPT_CACHED,
                    messages=messages_loop,
                    # tools 의도적으로 누락 → 최종 텍스트 응답 강제.
                    max_tokens=MAX_ANSWER_TOKENS,
                ) as fstream:
                    for event in fstream:
                        et = getattr(event, "type", "")
                        if et == "content_block_delta":
                            delta = getattr(event, "delta", None)
                            dt = getattr(delta, "type", "") if delta else ""
                            if dt == "text_delta":
                                accumulated_answer.append(delta.text)
                                yield _sse("token", {"text": delta.text})
                    final_synth = fstream.get_final_message()
                    usage = final_synth.usage
                    for k in ("input_tokens", "output_tokens",
                              "cache_creation_input_tokens", "cache_read_input_tokens"):
                        v = getattr(usage, k, None) or 0
                        cache_stats[k] = (cache_stats.get(k) or 0) + v
        except RateLimitError as exc:
            # SDK 자동 재시도(max_retries=4)도 못 뚫은 ITPM 한도. 사용자에게 명확히 안내.
            retry_after = None
            try:
                retry_after = exc.response.headers.get("retry-after")
            except Exception:
                pass
            logger.warning("agent streaming rate_limited (retry-after=%s)", retry_after)
            yield _sse("rate_limited", {
                "message": "Anthropic API 분당 토큰 한도 초과. 잠시 후 다시 시도해 주세요.",
                "retry_after_sec": retry_after,
            })
            return
        except Exception as exc:
            logger.exception("agent streaming 실패")
            yield _sse("error", {"message": f"{type(exc).__name__}: {exc}"})
            return

        t_generate = time.time() - t2
        answer_full = "".join(accumulated_answer)

        # Phase 7: conversation history 저장 (다음 turn 활용)
        update_conversation(conv_id, req.question, answer_full)

        # Phase 5: self-critique + deterministic citation verifier → SSE events
        t3 = time.time()
        critique = critique_answer(req.question, context, answer_full)
        verifier = verify_citations(answer_full, top)
        if _critique_needs_adversarial(critique, verifier):
            adv = adversarial_critique(req.question, context, answer_full, critique)
            critique = _merge_critiques(critique, adv)
        t_critique = time.time() - t3
        yield _sse("critique", critique)
        yield _sse("verifier", verifier)

        # Phase 5b: critique/verifier-driven retry (streaming). agent loop 한 번 더 — tool 사용 허용.
        # client에는 'regenerating' 이벤트 먼저 보내 답변 영역을 reset하도록 신호.
        retry_count = 0
        retry_reason = "ok"
        t_retry = 0.0
        do_retry, retry_reason = should_retry(critique, verifier)
        if do_retry:
            retry_count = 1
            retry_msg = build_retry_user_message(req.question, context, answer_full, critique, verifier)
            yield _sse("regenerating", {
                "reason": retry_reason,
                "critique": critique,
            })
            t_retry_start = time.time()
            try:
                # 새 agent loop. history는 retry용으로 비우고 retry message 단독.
                messages_retry: list[dict[str, Any]] = [{"role": "user", "content": retry_msg}]
                accumulated_retry: list[str] = []
                tool_calls_retry: list[dict[str, Any]] = []
                iter_count_retry = 0
                for iter_n in range(MAX_TOOL_ITERATIONS):
                    iter_count_retry = iter_n + 1
                    with client.messages.stream(
                        model=CLAUDE_MODEL,
                        system=SYSTEM_PROMPT_CACHED,
                        messages=messages_retry,
                        tools=TOOLS,
                        **_agent_loop_kwargs(),
                    ) as rstream:
                        for event in rstream:
                            et = getattr(event, "type", "")
                            if et == "content_block_delta":
                                delta = getattr(event, "delta", None)
                                if delta and getattr(delta, "type", "") == "text_delta":
                                    accumulated_retry.append(delta.text)
                                    yield _sse("token", {"text": delta.text})
                        final_r = rstream.get_final_message()
                        ur = final_r.usage
                        for k in ("input_tokens", "output_tokens",
                                  "cache_creation_input_tokens", "cache_read_input_tokens"):
                            v = getattr(ur, k, None) or 0
                            cache_stats[k] = (cache_stats.get(k) or 0) + v
                    if getattr(final_r, "stop_reason", None) != "tool_use":
                        break
                    messages_retry.append({"role": "assistant", "content": final_r.content})
                    tr_payload: list[dict[str, Any]] = []
                    for block in final_r.content:
                        if getattr(block, "type", "") != "tool_use":
                            continue
                        tname = block.name
                        tinput = block.input
                        yield _sse("tool_call", {"name": tname, "input": tinput, "iteration": iter_count_retry, "phase": "retry"})
                        t_tool = time.time()
                        result = execute_tool(tname, tinput)
                        elapsed_tool = round(time.time() - t_tool, 2)
                        result_str = json.dumps(result, ensure_ascii=False)
                        tool_calls_retry.append({
                            "name": tname, "input": tinput,
                            "result_size": len(result_str), "elapsed_sec": elapsed_tool,
                            "result_count": result.get("count") if isinstance(result, dict) else None,
                            "phase": "retry",
                        })
                        yield _sse("tool_result", {
                            "name": tname,
                            "result_count": result.get("count") if isinstance(result, dict) else None,
                            "elapsed_sec": elapsed_tool,
                            "phase": "retry",
                        })
                        tr_payload.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result_str,
                        })
                    messages_retry.append({"role": "user", "content": tr_payload})
                else:
                    # retry agent loop도 iter 한도 도달 → tools 빼고 최종 합성 강제.
                    logger.info("retry agent loop hit MAX_TOOL_ITERATIONS, forcing synthesis")
                    yield _sse("iter_limit_synthesis", {"message": "재시도 마무리 — 최종 답변 합성 중", "iterations": iter_count_retry, "phase": "retry"})
                    messages_retry.append({
                        "role": "user",
                        "content": "추가 도구 호출 없이, 지금까지 확보된 자료만 근거로 한국어 최종 답변을 작성하세요.",
                    })
                    with client.messages.stream(
                        model=CLAUDE_MODEL,
                        system=SYSTEM_PROMPT_CACHED,
                        messages=messages_retry,
                        max_tokens=MAX_ANSWER_TOKENS,
                    ) as fstream2:
                        for event in fstream2:
                            et = getattr(event, "type", "")
                            if et == "content_block_delta":
                                delta = getattr(event, "delta", None)
                                if delta and getattr(delta, "type", "") == "text_delta":
                                    accumulated_retry.append(delta.text)
                                    yield _sse("token", {"text": delta.text})
                        final_synth2 = fstream2.get_final_message()
                        us2 = final_synth2.usage
                        for k in ("input_tokens", "output_tokens",
                                  "cache_creation_input_tokens", "cache_read_input_tokens"):
                            v = getattr(us2, k, None) or 0
                            cache_stats[k] = (cache_stats.get(k) or 0) + v
                answer_retry = "".join(accumulated_retry).strip()
                if answer_retry:
                    answer_full = answer_retry
                    # conversation history도 retry 답변으로 갱신.
                    update_conversation(conv_id, req.question, answer_full)
                    tool_calls_made.extend(tool_calls_retry)
                    iter_count += iter_count_retry
                    # 재검증
                    critique = critique_answer(req.question, context, answer_full)
                    verifier = verify_citations(answer_full, top)
                    if _critique_needs_adversarial(critique, verifier):
                        adv = adversarial_critique(req.question, context, answer_full, critique)
                        critique = _merge_critiques(critique, adv)
                    yield _sse("critique", critique)
                    yield _sse("verifier", verifier)
            except RateLimitError as exc:
                # retry 단계 rate limit — 1차 답변은 이미 있으므로 retry 포기하고 1차 답변 유지.
                retry_after = None
                try:
                    retry_after = exc.response.headers.get("retry-after")
                except Exception:
                    pass
                logger.warning("stream retry rate_limited (retry-after=%s), keeping initial answer", retry_after)
                yield _sse("rate_limited", {
                    "message": "재시도 단계에서 API 한도 초과 — 1차 답변을 유지합니다.",
                    "retry_after_sec": retry_after,
                    "phase": "retry",
                })
            except Exception as exc:
                logger.warning("stream retry 실패: %s", exc)
                yield _sse("error", {"message": f"retry 중 오류: {type(exc).__name__}: {exc}"})
            t_retry = time.time() - t_retry_start

        yield _sse("done", {
            "debug": {
                **debug,
                "selected": len(top),
                "timing": {
                    "retrieve_sec": round(t_retrieve, 2),
                    "rerank_sec": round(t_rerank, 2),
                    "generate_sec": round(t_generate, 2),
                    "critique_sec": round(t_critique, 2),
                    "retry_sec": round(t_retry, 2),
                    "total_sec": round(time.time() - t0, 2),
                },
                "usage": cache_stats,
                "critique": critique,
                "verifier": verifier,
                "retry": {"count": retry_count, "reason": retry_reason},
                "agent": {
                    "iterations": iter_count,
                    "tool_calls": tool_calls_made,
                    "tool_call_count": len(tool_calls_made),
                },
                "conversation": {
                    "id": conv_id,
                    "turn": len(history_messages) // 2 + 1,
                    "history_messages_loaded": len(history_messages),
                },
            },
        })

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # cloudflared/nginx buffering 방지
        },
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("rag_server_v2:app", host="127.0.0.1", port=8002, reload=False)
