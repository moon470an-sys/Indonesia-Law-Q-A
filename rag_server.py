"""
인도네시아 헌법 RAG FastAPI 서버.

실행:
    uvicorn rag_server:app --reload
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# C: 디스크가 거의 가득 차 있어 무거운 캐시는 D:에 저장 (환경변수로 override 가능)
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
from pydantic import BaseModel, Field
from sentence_transformers import SentenceTransformer

PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env")

# ChromaDB 클라이언트 팩토리. HttpClient(기본) / PersistentClient 모드 분기는 rag_chroma 모듈에서.
from rag_chroma import (
    CHROMA_MODE,
    CHROMA_PATH as CHROMA_DIR,  # 기존 식별자 유지 (호환). /health 응답에서 dir 존재 여부 표시용.
    describe_target,
    get_chroma_client,
)
COLLECTION_NAME = "indonesia_constitution"
EMBEDDING_MODEL = os.getenv(
    "RAG_EMBEDDING_MODEL",
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
)
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
TOP_K_DEFAULT = 15
MAX_ANSWER_TOKENS = 4096

# Retrieval re-ranking. cosine distance에 weight을 곱해 effective distance를 만든 뒤 정렬한다.
# 작을수록 우대. dense retrieval만으로는 한국어 query가 모든 법령의 도입부(Menimbang) 페이지와
# 유사하게 매칭되는 문제가 있어, 위계/페이지 기반 weight으로 균형을 잡는다.

# 상위법 우선 (SYSTEM_PROMPT의 위계 규칙과 일치). 헌법은 488청크라 dense retrieval만으로는
# 거대 카테고리에 묻혀 0개 나오는 케이스가 관측됐음 (2026-05-11).
HIERARCHY_WEIGHTS = {
    "indonesia_constitution": 0.80,
    "indonesia_uu": 0.88,
    "indonesia_pp": 0.92,
    "indonesia_perpres": 0.94,
    "indonesia_permen": 0.96,
    "indonesia_kepmen": 0.97,
    "indonesia_perda": 0.97,
    "indonesia_lainnya": 1.00,
}
DEFAULT_HIERARCHY_WEIGHT = 1.0

# 사용자가 categories 미지정한 일반 query일 때만 적용. 위계 균형 보장.
MINIMUM_QUOTA = {
    "indonesia_constitution": 3,
    "indonesia_uu": 4,
    "indonesia_pp": 2,
}

# 도입부(표지/Menimbang/Penjelasan) 페이지는 거의 모든 법령에서 비슷한 문구가 반복되어
# 일반 query에서 잡힐 확률이 너무 높음. 본문(BAB II 이상, page≥3) 우선.
EARLY_PAGE_THRESHOLD = 2
EARLY_PAGE_PENALTY = 1.15

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.getenv(
        "ALLOWED_ORIGINS",
        "https://moon470an-sys.github.io,http://localhost:8501,http://127.0.0.1:8501",
    ).split(",")
    if o.strip()
]

# ===== 핫리로드 가능 정책 설정 =====
# 토큰 검증/세팅값처럼 자주 바꾸는 항목은 모듈 전역 상수로 굳히지 않고 dict로 둬서
# .env 수정 후 POST /admin/reload-config로 재시작 없이 적용한다.
# 모델 경로/ChromaDB 연결 같은 인프라성은 여전히 모듈 import 시점에 고정.
_config: dict[str, Any] = {}
_config_lock = threading.Lock()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _read_policy_env() -> dict[str, Any]:
    """현재 process env(이미 .env 로드된 상태)에서 핫리로드 대상 정책값을 읽어 dict."""
    # 토큰: 콤마 분리 다중 지원(RAG_TOKENS), 기존 단일 CLIENT_API_TOKEN도 인정.
    tokens_raw = os.getenv("RAG_TOKENS") or os.getenv("CLIENT_API_TOKEN") or ""
    tokens = {t.strip() for t in tokens_raw.split(",") if t.strip()}

    # 기본값: 토큰이 하나라도 있으면 검증 ON, 아니면 OFF.
    # 명시적으로 RAG_REQUIRE_TOKEN을 설정하면 그 값을 우선.
    require = _env_bool("RAG_REQUIRE_TOKEN", default=bool(tokens))

    return {
        "require_token": require,
        "tokens": frozenset(tokens),
    }


def reload_config() -> dict[str, Any]:
    """스레드 안전하게 _config를 .env로부터 재로드. 변경된 키 목록 반환.

    .env 파일을 다시 읽고(override=True) 그 결과를 process env에 반영한 뒤
    _read_policy_env로 정책값만 dict에 갱신한다.
    """
    # override=True: 이미 process env에 있는 값도 .env로 덮어쓰기. 토큰 비우기 등을
    # 적용하려면 필수. 단점: shell에서 export한 값도 덮어쓰지만, 운영 환경에선 .env가 단일 진실.
    load_dotenv(PROJECT_DIR / ".env", override=True)
    new_cfg = _read_policy_env()

    with _config_lock:
        changed: list[str] = []
        for k, v in new_cfg.items():
            if _config.get(k) != v:
                changed.append(k)
            _config[k] = v
        # 사라진 키 정리
        for k in list(_config.keys()):
            if k not in new_cfg:
                _config.pop(k, None)
                changed.append(f"-{k}")

    return {
        "reloaded": changed,
        "current": {
            "require_token": _config["require_token"],
            "token_count": len(_config["tokens"]),
        },
    }


# 부팅 시점에 1회 로드 (process env에 .env 반영 후 정책 dict 채움).
reload_config()

# 운영 호환을 위한 별칭. 코드를 import해서 쓰는 곳이 있으면 깨지지 않게.
CLIENT_API_TOKEN = ",".join(sorted(_config["tokens"])) if _config["tokens"] else ""

# ===== Admin endpoint 보호 키 (인프라성, 핫리로드 대상 아님) =====
ADMIN_KEY = os.getenv("RAG_ADMIN_KEY", "")


app = FastAPI(title="Indonesia Constitution RAG", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    allow_credentials=False,
)


def require_token(x_api_token: str | None = Header(default=None)) -> None:
    """프론트엔드에서 보낸 토큰 검증. _config dict을 매 요청마다 참조해 핫리로드 반영."""
    with _config_lock:
        require = bool(_config.get("require_token"))
        tokens: frozenset[str] = _config.get("tokens", frozenset())
    if not require:
        return
    if not tokens or x_api_token not in tokens:
        raise HTTPException(status_code=401, detail="invalid token")


def _require_admin(x_admin_key: str | None) -> None:
    if not ADMIN_KEY:
        # 키가 비어 있으면 admin endpoint 비활성화. 실수로 무방비 노출되는 일 차단.
        raise HTTPException(status_code=503, detail="admin endpoint disabled (RAG_ADMIN_KEY not set)")
    if x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="invalid admin key")

_state: dict[str, Any] = {
    "model": None,
    "client": None,
    "anthropic": None,
    "health_cache": None,        # 마지막 /health 결과
    "health_cache_ts": 0.0,       # 마지막 갱신 시각 (epoch)
    "ready": False,               # 워밍업 완료 여부 → /health/ready 200/503 분기
    "readiness_error": None,      # 워밍업 실패 시 사람이 읽을 수 있는 원인 한 줄
    "warmup_started": False,      # 중복 워밍업 가드
    "warmup_started_ts": 0.0,
    "warmup_finished_ts": 0.0,
}

HEALTH_CACHE_TTL = 86400.0  # 24h. 청크 수는 ingest가 돌 때만 변하고, ingest는 uvicorn 재시작을 동반 → 캐시도 자연히 초기화.
# 5분 같은 짧은 TTL이면 만료 후 frontend가 다시 warming=true를 받게 되고 자동 복구 경로가 길어짐.


def get_model() -> SentenceTransformer:
    if _state["model"] is None:
        _state["model"] = SentenceTransformer(EMBEDDING_MODEL)
    return _state["model"]


def get_client():
    """ChromaDB 클라이언트 (싱글톤).

    실제 인스턴스 종류(HttpClient / PersistentClient)는 RAG_CHROMA_MODE에 따라
    rag_chroma.get_chroma_client()가 결정한다. 컬렉션은 매번 list_collections로
    동적 조회.
    """
    if _state["client"] is None:
        _state["client"] = get_chroma_client()
    return _state["client"]


def list_indonesia_collections() -> list:
    """`indonesia_*` 패턴의 모든 컬렉션 반환. 새 카테고리가 인덱싱되면 자동 인식."""
    client = get_client()
    cols = []
    for c in client.list_collections():
        if c.name.startswith("indonesia_"):
            cols.append(c)
    return cols


def get_anthropic() -> Anthropic:
    if _state["anthropic"] is None:
        if not ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY가 설정되지 않았습니다. .env 파일을 확인하세요.")
        _state["anthropic"] = Anthropic(api_key=ANTHROPIC_API_KEY)
    return _state["anthropic"]


class QueryRequest(BaseModel):
    question: str = Field(..., min_length=1)
    top_k: int = Field(TOP_K_DEFAULT, ge=1, le=20)
    categories: list[str] | None = Field(
        default=None,
        description="검색을 한정할 카테고리 목록(폴더명 또는 컬렉션명). 비어있으면 전체.",
    )


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


SYSTEM_PROMPT = """당신은 인도네시아 법령 전문가입니다.
주어진 [참고 문서]만을 근거로 사용자 질문에 한국어로 답변하세요.

대상 법령 종류:
- UUD/헌법 (Konstitusi)
- UU 법률 (Undang-Undang)
- PP 정부령 (Peraturan Pemerintah)
- Perpres 대통령령 (Peraturan Presiden)
- Permen 장관령 (Peraturan Menteri)
- Kepmen 장관결정 (Keputusan Menteri)
- Perda 지방조례 (Peraturan Daerah)
- 기타 (Inpres 등)

규칙:
1. 답변은 반드시 한국어로 작성합니다.
2. 참고 문서에 근거가 없으면 "제공된 문서에서 해당 내용을 찾을 수 없습니다"라고 답합니다.
3. 답변 본문에서 인용한 조항을 (Pasal 6A, 출처: 파일명) 형식으로 표기합니다.
4. 답변 끝에 "출처:" 섹션에 사용된 출처(법령 종류 + 파일명 + 페이지 + 조항)를 모두 나열합니다.
5. 추측하지 말고 문서에 있는 사실만 답합니다.
6. 여러 법령이 충돌할 경우 상위법(헌법 > 법률 > 정부령 > 대통령령 > 장관령) 우선이며, 그 사실을 답변에 명시합니다.
"""


def build_context(docs: list[str], metas: list[dict]) -> str:
    blocks = []
    for i, (doc, meta) in enumerate(zip(docs, metas), start=1):
        article = meta.get("article") or "조항 미확인"
        source = meta.get("source", "?")
        page = meta.get("page", "?")
        category = meta.get("category") or ""
        cat_part = f", {category}" if category else ""
        blocks.append(
            f"[참고문서 {i}] (출처: {source}, p.{page}, {article}{cat_part})\n{doc}"
        )
    return "\n\n".join(blocks)


def search_all_collections(
    query_emb: list[list[float]], top_k: int, categories: list[str] | None,
) -> tuple[list[str], list[dict], list[float]]:
    """가중치 reranking + 위계 quota 적용한 multi-collection retrieval.

    각 컬렉션에서 fetch_k(= top_k*3) 후보를 가져와, 위계 가중치/페이지 페널티를 곱한
    effective distance로 정렬한다. 사용자가 categories 미지정한 경우에는 위계 quota를
    먼저 채워 헌법/법률이 거대 카테고리에 묻히는 현상을 방지한다.
    """
    cols = list_indonesia_collections()
    user_filtered = bool(categories)
    if categories:
        wanted = set()
        for c in categories:
            cl = c.strip().lower()
            if cl.startswith("indonesia_"):
                wanted.add(cl)
            else:
                # 폴더명/별칭으로 들어온 경우
                # "헌법" → "indonesia_constitution"
                from index_manager import normalize_category
                wanted.add(normalize_category(c.strip()))
        cols = [c for c in cols if c.name in wanted]

    fetch_k = max(top_k * 3, 20)
    candidates: list[tuple[float, float, str, dict, str]] = []

    for col in cols:
        try:
            res = col.query(query_embeddings=query_emb, n_results=fetch_k)
        except Exception:
            continue
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        col_weight = HIERARCHY_WEIGHTS.get(col.name, DEFAULT_HIERARCHY_WEIGHT)
        for doc, meta, dist in zip(docs, metas, dists):
            try:
                page_num = int(meta.get("page", 99) or 99)
            except (ValueError, TypeError):
                page_num = 99
            page_pen = EARLY_PAGE_PENALTY if page_num <= EARLY_PAGE_THRESHOLD else 1.0
            eff = (dist if dist is not None else 1.0) * col_weight * page_pen
            candidates.append((eff, dist, doc, meta, col.name))

    if not candidates:
        return [], [], []

    candidates.sort(key=lambda t: t[0])

    def _dedup_key(meta: dict) -> tuple:
        return (meta.get("source"), meta.get("page"), meta.get("article"))

    selected: list[tuple[float, float, str, dict, str]] = []
    seen: set = set()

    # Quota 먼저 — 사용자가 categories 미지정한 일반 query 에서만.
    # categories 지정한 경우엔 그 카테고리만 보고 싶다는 뜻이라 quota 강제 안 함.
    if not user_filtered:
        for col_name, quota in MINIMUM_QUOTA.items():
            taken = 0
            for tup in candidates:
                if tup[4] != col_name:
                    continue
                k = _dedup_key(tup[3])
                if k in seen:
                    continue
                selected.append(tup)
                seen.add(k)
                taken += 1
                if taken >= quota:
                    break

    # 나머지를 effective distance 기준으로 채움
    for tup in candidates:
        if len(selected) >= top_k:
            break
        k = _dedup_key(tup[3])
        if k in seen:
            continue
        selected.append(tup)
        seen.add(k)

    # quota 합계가 top_k를 초과하면 잘라낸다 (작은 top_k 케이스 보호)
    selected = selected[:top_k]
    # 최종 정렬: effective distance 오름차순으로 사용자에게 표시
    selected.sort(key=lambda t: t[0])

    docs_out = [t[2] for t in selected]
    metas_out = [t[3] for t in selected]
    # 원본 distance 반환 — /query 응답의 score는 사용자가 이해하기 쉬운 raw cosine 기준.
    dists_out = [t[1] for t in selected]
    return docs_out, metas_out, dists_out


def _warmup_sync() -> None:
    """SentenceTransformer + ChromaDB를 백그라운드 스레드에서 미리 로드.

    완료 시 _state["ready"] = True. 실패 시 readiness_error에 원인을 적고 False 유지.
    watchdog는 /health/live만 polling하면 되므로 워밍업 중에도 죽이지 않음.
    """
    _state["warmup_started"] = True
    _state["warmup_started_ts"] = time.time()
    started = time.perf_counter()
    try:
        logger.info("warmup: SentenceTransformer 로딩 시작 (%s)", EMBEDDING_MODEL)
        m = get_model()
        m.encode(["warmup"], convert_to_numpy=True, normalize_embeddings=True)
        logger.info("warmup: SentenceTransformer OK (%.1fs)", time.perf_counter() - started)

        t1 = time.perf_counter()
        logger.info("warmup: ChromaDB 연결 검증 (target=%s)", describe_target())
        cols = list_indonesia_collections()
        for c in cols:
            # peek은 count()보다 가볍게 컬렉션 접근만 확인 — readiness 용도엔 충분.
            try:
                c.peek(limit=1)
            except Exception:
                # peek 미지원/실패해도 컬렉션 리스트 자체는 받았으니 ready 인정.
                pass
        logger.info(
            "warmup: ChromaDB OK, %d collections (%.1fs)",
            len(cols), time.perf_counter() - t1,
        )

        _state["ready"] = True
        _state["readiness_error"] = None
        _state["warmup_finished_ts"] = time.time()
        logger.info("warmup: 완료, 총 %.1fs", time.perf_counter() - started)
    except Exception as exc:
        _state["ready"] = False
        _state["readiness_error"] = f"{type(exc).__name__}: {exc}"
        _state["warmup_finished_ts"] = time.time()
        logger.exception("warmup 실패")


@app.on_event("startup")
async def _on_startup() -> None:
    """uvicorn 부팅 직후 워밍업을 백그라운드 스레드로 발사.

    asyncio.to_thread로 위임해서 이벤트루프를 막지 않음. /healthz / /health/live는
    워밍업 진행과 무관하게 즉시 200을 반환. /health/ready만 워밍업 끝나면 200, 그 전엔 503.
    """
    import asyncio
    if _state.get("warmup_started"):
        return
    asyncio.create_task(asyncio.to_thread(_warmup_sync))


@app.get("/health/live")
def health_live() -> dict[str, Any]:
    """Liveness — 프로세스 살아있음. 의존성 체크 없음. watchdog 용도."""
    return {"ok": True, "alive": True}


@app.get("/health/ready")
def health_ready():
    """Readiness — 모든 의존성(SentenceTransformer + ChromaDB) 로딩 완료 여부.

    프록시/프론트엔드가 사용자 요청 라우팅 가능한지 판단할 때 사용.
    """
    from fastapi.responses import JSONResponse
    ready = bool(_state.get("ready"))
    body = {
        "ready": ready,
        "error": _state.get("readiness_error"),
        "warmup_started_ts": _state.get("warmup_started_ts") or None,
        "warmup_finished_ts": _state.get("warmup_finished_ts") or None,
        "chroma_target": describe_target(),
    }
    return JSONResponse(body, status_code=200 if ready else 503)


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    """호환 alias for /health/live. 기존 watchdog/cloudflared 설정과의 호환을 위해 유지."""
    return health_live()


@app.post("/admin/reload-config")
def admin_reload_config(x_admin_key: str | None = Header(default=None)) -> dict[str, Any]:
    """정책성 설정을 .env로부터 재시작 없이 다시 읽는다.

    핫리로드 대상: RAG_REQUIRE_TOKEN, RAG_TOKENS (또는 CLIENT_API_TOKEN).
    인프라성(ChromaDB 연결, 모델 경로, ALLOWED_ORIGINS 등)은 대상 아님 — 재시작 필요.

    인증: X-Admin-Key 헤더가 RAG_ADMIN_KEY와 일치해야 함. RAG_ADMIN_KEY 자체가
    비어있으면 503을 반환해서 admin endpoint를 비활성화.
    """
    _require_admin(x_admin_key)
    return reload_config()


@app.get("/health")
def health(quick: int = 0) -> dict[str, Any]:
    """기본 모드: 캐시 fresh면 캐시, 아니면 동기 count() (cold 80s+).

    quick=1: 캐시가 있으면(stale 포함) 그걸 반환, 정말 없을 때만 `warming=true` 즉답.
      watchdog가 uvicorn healthy 직후 별도로 일반 /health를 호출해 캐시를 채워둠.
      stale을 응답하더라도 청크 수 표시용으로는 충분함. 변동은 ingest 후에만 발생.
    """
    cached = _state.get("health_cache")
    cached_ts = _state.get("health_cache_ts", 0.0)
    cache_fresh = cached is not None and (time.time() - cached_ts) < HEALTH_CACHE_TTL

    if cache_fresh:
        return cached

    if quick:
        # 캐시 fresh가 아닐 때: stale이라도 있으면 반환 (사용자 경험 우선).
        # 정말 캐시가 없을 때만 warming=true.
        if cached is not None:
            return {**cached, "stale": True}
        return {
            "ok": True,
            "warming": True,
            "chroma_mode": CHROMA_MODE,
            "chroma_target": describe_target(),
            "anthropic_key_set": bool(ANTHROPIC_API_KEY),
        }

    # 동기 모드 (캐시 채움까지 대기). FastAPI sync def → anyio threadpool에서 실행 → 이벤트루프 안 막음.
    info: dict[str, Any] = {
        "ok": True,
        "chroma_mode": CHROMA_MODE,
        "chroma_target": describe_target(),
        "anthropic_key_set": bool(ANTHROPIC_API_KEY),
    }
    try:
        cols = list_indonesia_collections()
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


@app.post("/query", response_model=QueryResponse)
def query(req: QueryRequest, x_api_token: str | None = Header(default=None)) -> QueryResponse:
    require_token(x_api_token)
    try:
        model = get_model()
        client = get_anthropic()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    query_emb = model.encode(
        [req.question],
        convert_to_numpy=True,
        normalize_embeddings=True,
    ).tolist()

    docs, metas, distances = search_all_collections(
        query_emb, req.top_k, req.categories,
    )

    if not docs:
        return QueryResponse(
            answer="제공된 문서에서 관련 내용을 찾을 수 없습니다.",
            sources=[],
        )

    context = build_context(docs, metas)
    user_message = (
        f"[참고 문서]\n{context}\n\n"
        f"[질문]\n{req.question}\n\n"
        "위 참고 문서를 근거로 한국어로 답변해 주세요. 인용한 조항 번호와 출처를 함께 표시하세요."
    )

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=MAX_ANSWER_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )
    answer_parts = [
        block.text for block in response.content if getattr(block, "type", None) == "text"
    ]
    answer = "\n".join(answer_parts).strip() or "(빈 응답)"

    sources = []
    for doc, meta, dist in zip(docs, metas, distances):
        sources.append(
            SourceItem(
                source=str(meta.get("source", "?")),
                page=int(meta.get("page", 0) or 0),
                article=str(meta.get("article") or ""),
                category=str(meta.get("category") or ""),
                score=float(1.0 - dist) if dist is not None else 0.0,
                snippet=doc[:240] + ("…" if len(doc) > 240 else ""),
            )
        )

    return QueryResponse(answer=answer, sources=sources)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("rag_server:app", host="127.0.0.1", port=8000, reload=True)
