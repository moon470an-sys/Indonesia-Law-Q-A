"""
인도네시아 헌법 RAG FastAPI 서버.

실행:
    uvicorn rag_server:app --reload
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

# C: 디스크가 거의 가득 차 있어 무거운 캐시는 D:에 저장 (환경변수로 override 가능)
_DEFAULT_CACHE = Path(os.getenv("RAG_MODEL_CACHE", r"D:\hf_cache"))
_DEFAULT_CACHE.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("HF_HOME", str(_DEFAULT_CACHE))
os.environ.setdefault("HUGGINGFACE_HUB_CACHE", str(_DEFAULT_CACHE / "hub"))
os.environ.setdefault("TRANSFORMERS_CACHE", str(_DEFAULT_CACHE / "transformers"))
os.environ.setdefault("SENTENCE_TRANSFORMERS_HOME", str(_DEFAULT_CACHE / "sentence_transformers"))
os.environ.setdefault("TORCH_HOME", str(_DEFAULT_CACHE / "torch"))

import chromadb
from anthropic import Anthropic
from chromadb.config import Settings
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sentence_transformers import SentenceTransformer

PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env")

CHROMA_DIR = Path(os.getenv("RAG_CHROMA_DIR", r"D:\rag_data\chroma_db"))
COLLECTION_NAME = "indonesia_constitution"
EMBEDDING_MODEL = os.getenv(
    "RAG_EMBEDDING_MODEL",
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
)
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
TOP_K_DEFAULT = 5

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
CLIENT_API_TOKEN = os.getenv("CLIENT_API_TOKEN", "")
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.getenv(
        "ALLOWED_ORIGINS",
        "https://moon470an-sys.github.io,http://localhost:8501,http://127.0.0.1:8501",
    ).split(",")
    if o.strip()
]

app = FastAPI(title="Indonesia Constitution RAG", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    allow_credentials=False,
)


def require_token(x_api_token: str | None = Header(default=None)) -> None:
    """프론트엔드(GitHub Pages)에서 보낸 토큰 검증.

    CLIENT_API_TOKEN을 비워두면 검증을 건너뜁니다(로컬 개발 시).
    """
    if not CLIENT_API_TOKEN:
        return
    if x_api_token != CLIENT_API_TOKEN:
        raise HTTPException(status_code=401, detail="invalid token")

_state: dict[str, Any] = {
    "model": None,
    "client": None,
    "anthropic": None,
    "health_cache": None,        # 마지막 /health 결과
    "health_cache_ts": 0.0,       # 마지막 갱신 시각 (epoch)
    "health_warming": False,      # 백그라운드 prewarm 진행 중 플래그
}
_health_lock = threading.Lock()

HEALTH_CACHE_TTL = 300.0  # /health 캐시 TTL (5분). count()가 80s+ 걸리는 cold start 비용을 사용자에게 노출시키지 않음.


def get_model() -> SentenceTransformer:
    if _state["model"] is None:
        _state["model"] = SentenceTransformer(EMBEDDING_MODEL)
    return _state["model"]


def get_client():
    """ChromaDB PersistentClient (싱글톤). 컬렉션은 매번 list_collections로 동적 조회."""
    if _state["client"] is None:
        if not CHROMA_DIR.exists():
            raise RuntimeError(
                f"ChromaDB 디렉토리가 없습니다: {CHROMA_DIR}. 먼저 인덱싱 스크립트를 실행하세요."
            )
        _state["client"] = chromadb.PersistentClient(
            path=str(CHROMA_DIR),
            settings=Settings(anonymized_telemetry=False),
        )
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
    """모든 indonesia_* 컬렉션에서 top_k씩 가져와 거리 기준으로 통합 후 상위 top_k 반환."""
    cols = list_indonesia_collections()
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

    all_docs: list[str] = []
    all_metas: list[dict] = []
    all_dists: list[float] = []

    for col in cols:
        try:
            res = col.query(query_embeddings=query_emb, n_results=top_k)
        except Exception:
            continue
        docs = (res.get("documents") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        all_docs.extend(docs)
        all_metas.extend(metas)
        all_dists.extend(dists)

    if not all_docs:
        return [], [], []

    # 거리 오름차순 정렬 후 top_k
    order = sorted(range(len(all_dists)), key=lambda i: all_dists[i])[:top_k]
    return [all_docs[i] for i in order], [all_metas[i] for i in order], [all_dists[i] for i in order]


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    # 가벼운 liveness 체크. ChromaDB 접근 안 함 → watchdog/cloudflared가 빠르게 살아있음 확인.
    return {"ok": True}


def _compute_full_health() -> dict[str, Any]:
    info: dict[str, Any] = {
        "ok": True,
        "chroma_dir_exists": CHROMA_DIR.exists(),
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
    return info


def _refresh_health_cache() -> None:
    """count()를 백그라운드 스레드에서 호출해 캐시 채움. /health?quick=1 즉답 직후 발사."""
    with _health_lock:
        if _state.get("health_warming"):
            return
        _state["health_warming"] = True
    try:
        info = _compute_full_health()
        if info.get("ok"):
            _state["health_cache"] = info
            _state["health_cache_ts"] = time.time()
    finally:
        _state["health_warming"] = False


@app.get("/health")
def health(quick: int = 0) -> dict[str, Any]:
    """기본 모드: 캐시 있으면 캐시, 없으면 동기 count() (cold 80s).

    quick=1: 캐시 있으면 캐시 즉답, 없으면 `warming=true`로 즉답 + 백그라운드 prewarm 시작.
    프론트는 quick=1 → warming 응답 받으면 잠시 후 일반 /health로 재호출.
    """
    cached = _state.get("health_cache")
    cached_ts = _state.get("health_cache_ts", 0.0)
    cache_fresh = cached is not None and (time.time() - cached_ts) < HEALTH_CACHE_TTL

    if cache_fresh:
        return cached

    if quick:
        # 캐시 없음/만료 → 백그라운드 prewarm 발사 후 즉답
        if not _state.get("health_warming"):
            threading.Thread(target=_refresh_health_cache, daemon=True).start()
        return {
            "ok": True,
            "warming": True,
            "chroma_dir_exists": CHROMA_DIR.exists(),
            "anthropic_key_set": bool(ANTHROPIC_API_KEY),
        }

    # 동기 모드 (캐시 채움까지 대기)
    info = _compute_full_health()
    if info.get("ok"):
        _state["health_cache"] = info
        _state["health_cache_ts"] = time.time()
    return info


@app.post("/health/prewarm")
def health_prewarm() -> dict[str, Any]:
    """watchdog가 uvicorn 기동 직후 호출. 백그라운드 prewarm 트리거. 즉답."""
    if _state.get("health_cache") is None and not _state.get("health_warming"):
        threading.Thread(target=_refresh_health_cache, daemon=True).start()
    return {"ok": True, "warming": _state.get("health_warming", False)}


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
        max_tokens=1024,
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
