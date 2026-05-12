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

from anthropic import Anthropic
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

TOP_K_DEFAULT = 15
FETCH_PER_QUERY = 30        # 각 query embedding당 가져올 후보 수
RERANK_POOL_SIZE = 50       # reranker에 보낼 후보 수
MAX_ANSWER_TOKENS = 8192    # extended thinking budget + 실제 답변 token 합. thinking 2000 + 답변 ~6000.
# Extended thinking — Sonnet 4.6/Opus 4.7에서 답변 전 추론 단계 명시적으로 사용.
# 위계 충돌, 복합 비교, 다중 인용 같은 복잡 질문에서 답변 깊이가 크게 개선됨.
# budget_tokens는 thinking 단계에서 사용할 최대 토큰 — 적당히 두면 비용/지연 균형.
THINKING_BUDGET_TOKENS = 2000

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
        _state["anthropic"] = Anthropic(api_key=ANTHROPIC_API_KEY)
    return _state["anthropic"]


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
    for (label, _qtext), emb in zip(queries, embeddings):
        for col in cols:
            try:
                res = col.query(query_embeddings=[emb], n_results=fetch_per)
            except Exception as exc:
                logger.warning("col %s query 실패: %s", col.name, exc)
                continue
            ids = (res.get("ids") or [[]])[0]
            docs = (res.get("documents") or [[]])[0]
            metas = (res.get("metadatas") or [[]])[0]
            dists = (res.get("distances") or [[]])[0]
            col_weight = HIERARCHY_WEIGHTS.get(col.name, DEFAULT_HIERARCHY_WEIGHT)
            # auto_categories에 포함된 컬렉션이면 0.92 가중치 추가 적용 (boost)
            if auto_categories and col.name in auto_categories:
                col_weight *= 0.92
            for cid, doc, meta, dist in zip(ids, docs, metas, dists):
                try:
                    page_num = int(meta.get("page", 99) or 99)
                except (ValueError, TypeError):
                    page_num = 99
                page_pen = EARLY_PAGE_PENALTY if page_num <= EARLY_PAGE_THRESHOLD else 1.0
                eff = (dist if dist is not None else 1.0) * col_weight * page_pen
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

    cand_list = sorted(candidates.values(), key=lambda c: c["eff_dist"])[: strat["rerank_pool"]]
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
        "candidates_unique": len(candidates),
        "candidates_topN": len(cand_list),
        "auto_categories": auto_categories,
    }
    return cand_list, debug, analysis


# ===== LLM-as-reranker =====

# ===== Phase 6: Agentic Tool Use =====
# Claude가 답변 도중 추가 정보가 필요하면 직접 도구를 호출.
# 두 가지 핵심 도구:
#   1) search_collection — 특정 카테고리에서 추가 검색
#   2) fetch_article_chunks — 특정 PDF + 조항 직접 조회

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
    """Phase 4: Claude Haiku로 candidates reranking + reasoning + diversity penalty.

    각 candidate에 'llm_score', 'llm_reason'을 추가. caller가 답변 단계에서 reasoning을 활용.
    """
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

    # Phase 5: self-critique
    t3 = time.time()
    critique = critique_answer(req.question, context, answer)
    t_critique = time.time() - t3

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
                "total_sec": round(time.time() - t0, 2),
            },
            "usage": cache_stats,
            "critique": critique,
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

    candidates, debug, analysis = multi_query_retrieve(req.question, req.top_k, req.categories)
    t_retrieve = time.time() - t0

    def event_gen():
        # conversation_id 먼저 보냄 — frontend가 다음 turn에 이 id 재사용
        yield _sse("conversation", {
            "conversation_id": conv_id,
            "turn": len(history_messages) // 2 + 1,
            "is_followup": len(history_messages) > 0,
        })

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
                    max_tokens=MAX_ANSWER_TOKENS,
                    thinking={"type": "enabled", "budget_tokens": THINKING_BUDGET_TOKENS},
                    system=SYSTEM_PROMPT_CACHED,
                    messages=messages_loop,
                    tools=TOOLS,
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
        except Exception as exc:
            logger.exception("agent streaming 실패")
            yield _sse("error", {"message": f"{type(exc).__name__}: {exc}"})
            return

        t_generate = time.time() - t2
        answer_full = "".join(accumulated_answer)

        # Phase 7: conversation history 저장 (다음 turn 활용)
        update_conversation(conv_id, req.question, answer_full)

        # Phase 5: self-critique → SSE 'critique' event
        t3 = time.time()
        critique = critique_answer(req.question, context, answer_full)
        t_critique = time.time() - t3
        yield _sse("critique", critique)

        yield _sse("done", {
            "debug": {
                **debug,
                "selected": len(top),
                "timing": {
                    "retrieve_sec": round(t_retrieve, 2),
                    "rerank_sec": round(t_rerank, 2),
                    "generate_sec": round(t_generate, 2),
                    "critique_sec": round(t_critique, 2),
                    "total_sec": round(time.time() - t0, 2),
                },
                "usage": cache_stats,
                "critique": critique,
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
