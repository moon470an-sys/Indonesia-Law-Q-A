"""
인도네시아 헌법 PDF 문서를 읽어 ChromaDB에 임베딩하는 스크립트.

실행:
    python ingest.py
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Iterable

# C: 디스크가 거의 가득 차 있어 무거운 캐시는 D:에 저장 (환경변수로 override 가능)
_DEFAULT_CACHE = Path(os.getenv("RAG_MODEL_CACHE", r"D:\hf_cache"))
_DEFAULT_CACHE.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("HF_HOME", str(_DEFAULT_CACHE))
os.environ.setdefault("HUGGINGFACE_HUB_CACHE", str(_DEFAULT_CACHE / "hub"))
os.environ.setdefault("TRANSFORMERS_CACHE", str(_DEFAULT_CACHE / "transformers"))
os.environ.setdefault("SENTENCE_TRANSFORMERS_HOME", str(_DEFAULT_CACHE / "sentence_transformers"))
os.environ.setdefault("TORCH_HOME", str(_DEFAULT_CACHE / "torch"))

import pdfplumber
from sentence_transformers import SentenceTransformer

# rag_chroma 헬퍼: HttpClient(기본) / PersistentClient 모드 분기.
from rag_chroma import CHROMA_MODE, CHROMA_PATH as CHROMA_DIR, describe_target, get_chroma_client

SOURCE_DIR = Path(
    os.getenv("RAG_SOURCE_DIR", r"D:\인도네시아 법령 원문\헌법")
)
PROJECT_DIR = Path(__file__).resolve().parent
COLLECTION_NAME = "indonesia_constitution"
EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"

CHUNK_SIZE = 700      # 문자 단위
CHUNK_OVERLAP = 120


def extract_pdf_text(pdf_path: Path) -> list[tuple[int, str]]:
    pages: list[tuple[int, str]] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for idx, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            text = re.sub(r"[ \t]+", " ", text)
            text = re.sub(r"\n{2,}", "\n", text).strip()
            if text:
                pages.append((idx, text))
    return pages


ARTICLE_PATTERN = re.compile(
    r"(Pasal\s+\d+[A-Za-z]?|Bab\s+[IVXLCDM]+|Pembukaan|제\s*\d+\s*조)",
    re.IGNORECASE,
)


def detect_article(text: str) -> str | None:
    match = ARTICLE_PATTERN.search(text)
    if match:
        return match.group(0).strip()
    return None


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> Iterable[str]:
    if not text:
        return
    text = text.strip()
    if len(text) <= size:
        yield text
        return

    start = 0
    n = len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:
            window = text[start:end]
            split_at = max(window.rfind("\n"), window.rfind(". "), window.rfind("。"))
            if split_at > size * 0.5:
                end = start + split_at + 1
        yield text[start:end].strip()
        if end >= n:
            break
        start = max(end - overlap, start + 1)


def build_chunks(pdf_path: Path) -> list[dict]:
    pages = extract_pdf_text(pdf_path)
    chunks: list[dict] = []
    for page_num, page_text in pages:
        for piece in chunk_text(page_text):
            if not piece:
                continue
            chunks.append(
                {
                    "text": piece,
                    "source": pdf_path.name,
                    "page": page_num,
                    "article": detect_article(piece) or "",
                }
            )
    return chunks


def main() -> int:
    if not SOURCE_DIR.exists():
        print(f"[오류] 소스 폴더가 없습니다: {SOURCE_DIR}")
        return 1

    pdf_files = sorted(SOURCE_DIR.glob("*.pdf"))
    if not pdf_files:
        print(f"[오류] PDF 파일이 없습니다: {SOURCE_DIR}")
        return 1

    print(f"[1/4] 임베딩 모델 로딩: {EMBEDDING_MODEL}")
    print(f"      (HF cache: {os.environ.get('HF_HOME')})")
    model = SentenceTransformer(EMBEDDING_MODEL)

    print(f"[2/4] ChromaDB 초기화: mode={CHROMA_MODE} target={describe_target()}")
    # persistent 모드일 때만 디렉터리 생성. http 모드는 서버가 관리.
    if CHROMA_MODE == "persistent":
        CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    client = get_chroma_client()
    try:
        client.delete_collection(COLLECTION_NAME)
    except Exception:
        pass
    collection = client.create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )

    print(f"[3/4] PDF 파싱 및 청크 생성 ({len(pdf_files)}개 파일)")
    total_chunks = 0
    for pdf_path in pdf_files:
        chunks = build_chunks(pdf_path)
        if not chunks:
            print(f"  - {pdf_path.name}: 텍스트 추출 실패 또는 빈 문서")
            continue

        texts = [c["text"] for c in chunks]
        metadatas = [
            {"source": c["source"], "page": c["page"], "article": c["article"]}
            for c in chunks
        ]
        ids = [f"{pdf_path.stem}-p{c['page']}-{i}" for i, c in enumerate(chunks)]

        embeddings = model.encode(
            texts,
            batch_size=32,
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=True,
        ).tolist()

        collection.add(
            ids=ids,
            documents=texts,
            metadatas=metadatas,
            embeddings=embeddings,
        )
        total_chunks += len(chunks)
        print(f"  - {pdf_path.name}: 청크 {len(chunks)}개")

    print(f"[4/4] 임베딩 완료: 총 청크 {total_chunks}개 -> {CHROMA_DIR}")
    print("OK 임베딩 완료")
    return 0


if __name__ == "__main__":
    sys.exit(main())
