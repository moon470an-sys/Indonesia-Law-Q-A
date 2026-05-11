"""ChromaDB 클라이언트 팩토리 (HttpClient / PersistentClient 모드 분기).

분리 이유:
- PersistentClient는 uvicorn 프로세스가 죽으면 인덱스도 메모리에서 풀려서
  서버 재시작 비용이 10~30분. ChromaDB를 별도 데몬(`chroma run`)으로 띄우고
  HttpClient로 붙으면 uvicorn 재시작 = 즉시 끝.
- ingest 스크립트와 rag_server가 같은 헬퍼를 쓰도록 통일.

환경변수:
  RAG_CHROMA_MODE  "http"(기본) | "persistent"
  RAG_CHROMA_HOST  HttpClient 호스트 (기본 127.0.0.1)
  RAG_CHROMA_PORT  HttpClient 포트   (기본 8001 — uvicorn 8000과 분리)
  RAG_CHROMA_PATH  PersistentClient 디렉터리 (기본: 기존 RAG_CHROMA_DIR 값 또는
                   D:\\rag_data\\chroma_db). HttpClient 모드에서는 무시.

호환:
  RAG_CHROMA_MODE=persistent로 두면 기존 PersistentClient 동작 그대로.
  http 모드 배포가 실패해도 .env 한 줄 바꿔 즉시 롤백 가능.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import chromadb
from chromadb.config import Settings

logger = logging.getLogger(__name__)


def _normalize_mode(raw: str | None) -> str:
    mode = (raw or "http").strip().lower()
    if mode not in ("http", "persistent"):
        raise ValueError(
            f"RAG_CHROMA_MODE는 'http' 또는 'persistent'만 가능합니다 (받은 값: {raw!r})"
        )
    return mode


CHROMA_MODE: str = _normalize_mode(os.getenv("RAG_CHROMA_MODE"))
CHROMA_HOST: str = os.getenv("RAG_CHROMA_HOST", "127.0.0.1")
CHROMA_PORT: int = int(os.getenv("RAG_CHROMA_PORT", "8001"))
# RAG_CHROMA_PATH 우선, 기존 RAG_CHROMA_DIR도 호환으로 인정.
CHROMA_PATH: Path = Path(
    os.getenv("RAG_CHROMA_PATH")
    or os.getenv("RAG_CHROMA_DIR")
    or r"D:\rag_data\chroma_db"
)

_version_logged = False


def _log_version_compat(client: Any, mode: str, target: str) -> None:
    """프로세스 수명 동안 1회만 client/server 버전을 비교 로그.

    HttpClient에서 서버가 다른 chromadb 버전이면 컬렉션 포맷이 어긋날 수
    있으므로 시작 시 경고. 차이가 있어도 즉시 실패시키지는 않음.
    """
    global _version_logged
    if _version_logged:
        return
    _version_logged = True

    client_ver = getattr(chromadb, "__version__", "unknown")
    server_ver: str
    try:
        # HttpClient/PersistentClient 둘 다 .get_version() 지원
        server_ver = str(client.get_version())
    except Exception as exc:
        server_ver = f"<unreachable: {type(exc).__name__}: {exc}>"

    line = (
        f"ChromaDB mode={mode} target={target} "
        f"client_ver={client_ver} server_ver={server_ver}"
    )
    if not server_ver.startswith("<") and server_ver != client_ver:
        logger.warning("%s — version mismatch 가능", line)
    else:
        logger.info("%s", line)


def get_chroma_client() -> Any:
    """RAG_CHROMA_MODE 에 따라 HttpClient 또는 PersistentClient 반환.

    호출자는 결과를 캐싱(싱글톤)해서 재사용해야 합니다. 이 함수는 매번 새 인스턴스를
    생성합니다.
    """
    if CHROMA_MODE == "http":
        client = chromadb.HttpClient(
            host=CHROMA_HOST,
            port=CHROMA_PORT,
            settings=Settings(anonymized_telemetry=False),
        )
        _log_version_compat(client, "http", f"{CHROMA_HOST}:{CHROMA_PORT}")
        return client

    # persistent fallback
    if not CHROMA_PATH.exists():
        raise RuntimeError(
            f"ChromaDB 디렉토리 없음: {CHROMA_PATH}. 인덱싱을 먼저 실행하거나 "
            "RAG_CHROMA_MODE=http로 chroma run 서버를 사용하세요."
        )
    client = chromadb.PersistentClient(
        path=str(CHROMA_PATH),
        settings=Settings(anonymized_telemetry=False),
    )
    _log_version_compat(client, "persistent", str(CHROMA_PATH))
    return client


def describe_target() -> str:
    """로깅/디버깅용으로 현재 모드와 타겟을 한 줄로 반환."""
    if CHROMA_MODE == "http":
        return f"http://{CHROMA_HOST}:{CHROMA_PORT}"
    return f"persistent:{CHROMA_PATH}"
