"""
인도네시아 헌법 RAG FastAPI 서버.

실행:
    uvicorn rag_server:app --reload
"""
from __future__ import annotations

import os
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
EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"
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
    "collection": None,
    "anthropic": None,
}


def get_model() -> SentenceTransformer:
    if _state["model"] is None:
        _state["model"] = SentenceTransformer(EMBEDDING_MODEL)
    return _state["model"]


def get_collection():
    if _state["collection"] is None:
        if not CHROMA_DIR.exists():
            raise RuntimeError(
                f"ChromaDB 디렉토리가 없습니다: {CHROMA_DIR}. 먼저 `python ingest.py`를 실행하세요."
            )
        client = chromadb.PersistentClient(
            path=str(CHROMA_DIR),
            settings=Settings(anonymized_telemetry=False),
        )
        _state["collection"] = client.get_collection(COLLECTION_NAME)
    return _state["collection"]


def get_anthropic() -> Anthropic:
    if _state["anthropic"] is None:
        if not ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY가 설정되지 않았습니다. .env 파일을 확인하세요.")
        _state["anthropic"] = Anthropic(api_key=ANTHROPIC_API_KEY)
    return _state["anthropic"]


class QueryRequest(BaseModel):
    question: str = Field(..., min_length=1)
    top_k: int = Field(TOP_K_DEFAULT, ge=1, le=15)


class SourceItem(BaseModel):
    source: str
    page: int
    article: str
    score: float
    snippet: str


class QueryResponse(BaseModel):
    answer: str
    sources: list[SourceItem]


SYSTEM_PROMPT = """당신은 인도네시아 헌법 전문가입니다.
주어진 [참고 문서]만을 근거로 사용자 질문에 한국어로 답변하세요.

규칙:
1. 답변은 반드시 한국어로 작성합니다.
2. 참고 문서에 근거가 없으면 "제공된 문서에서 해당 내용을 찾을 수 없습니다"라고 답합니다.
3. 답변 본문에서 인용한 조항을 (Pasal 6A, 출처: UUD_1945_통합본) 형식으로 표기합니다.
4. 답변 끝에 "출처:" 섹션을 만들어 사용된 출처를 모두 나열합니다.
5. 추측하지 말고 문서에 있는 사실만 답합니다.
"""


def build_context(docs: list[str], metas: list[dict]) -> str:
    blocks = []
    for i, (doc, meta) in enumerate(zip(docs, metas), start=1):
        article = meta.get("article") or "조항 미확인"
        source = meta.get("source", "?")
        page = meta.get("page", "?")
        blocks.append(
            f"[참고문서 {i}] (출처: {source}, p.{page}, {article})\n{doc}"
        )
    return "\n\n".join(blocks)


@app.get("/health")
def health() -> dict[str, Any]:
    info: dict[str, Any] = {
        "ok": True,
        "chroma_dir_exists": CHROMA_DIR.exists(),
        "anthropic_key_set": bool(ANTHROPIC_API_KEY),
    }
    try:
        info["collection_count"] = get_collection().count()
    except Exception as exc:
        info["ok"] = False
        info["error"] = str(exc)
    return info


@app.post("/query", response_model=QueryResponse)
def query(req: QueryRequest, x_api_token: str | None = Header(default=None)) -> QueryResponse:
    require_token(x_api_token)
    try:
        model = get_model()
        collection = get_collection()
        client = get_anthropic()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    query_emb = model.encode(
        [req.question],
        convert_to_numpy=True,
        normalize_embeddings=True,
    ).tolist()

    results = collection.query(
        query_embeddings=query_emb,
        n_results=req.top_k,
    )

    docs = results.get("documents", [[]])[0]
    metas = results.get("metadatas", [[]])[0]
    distances = results.get("distances", [[]])[0]

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
                score=float(1.0 - dist) if dist is not None else 0.0,
                snippet=doc[:240] + ("…" if len(doc) > 240 else ""),
            )
        )

    return QueryResponse(answer=answer, sources=sources)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("rag_server:app", host="127.0.0.1", port=8000, reload=True)
