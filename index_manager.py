"""
인도네시아 법령 인덱싱 - 공통 모듈

manifest.json 기반 증분 처리:
- 파일 SHA-256 + size + mtime 추적
- 카테고리(폴더)별 ChromaDB 컬렉션 사용
- 신규/변경 파일만 처리, 삭제된 파일의 청크는 제거
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterable, Iterator

# C 디스크 절약: 모델/캐시 D 드라이브로
_DEFAULT_CACHE = Path(os.getenv("RAG_MODEL_CACHE", r"D:\hf_cache"))
_DEFAULT_CACHE.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("HF_HOME", str(_DEFAULT_CACHE))
os.environ.setdefault("HUGGINGFACE_HUB_CACHE", str(_DEFAULT_CACHE / "hub"))
os.environ.setdefault("TRANSFORMERS_CACHE", str(_DEFAULT_CACHE / "transformers"))
os.environ.setdefault("SENTENCE_TRANSFORMERS_HOME", str(_DEFAULT_CACHE / "sentence_transformers"))
os.environ.setdefault("TORCH_HOME", str(_DEFAULT_CACHE / "torch"))


SOURCE_ROOT = Path(os.getenv("RAG_SOURCE_ROOT", r"D:\인도네시아 법령 원문"))
CHROMA_DIR = Path(os.getenv("RAG_CHROMA_DIR", r"D:\rag_data\chroma_db"))
MANIFEST_PATH = Path(os.getenv("RAG_MANIFEST_PATH", r"D:\rag_data\manifest.json"))

# 폴더명 → 컬렉션명 매핑 (한글 부분이 없으면 latin 부분 그대로)
_CATEGORY_OVERRIDES = {
    "헌법": "constitution",
}


def normalize_category(folder_name: str) -> str:
    """폴더명을 ChromaDB 컬렉션명으로 정규화한다."""
    if folder_name in _CATEGORY_OVERRIDES:
        slug = _CATEGORY_OVERRIDES[folder_name]
    elif "_" in folder_name:
        # "법률_UU" → "uu"
        slug = folder_name.split("_", 1)[1]
    else:
        slug = folder_name
    slug = re.sub(r"[^A-Za-z0-9_]", "_", slug).strip("_").lower()
    return f"indonesia_{slug}"


def discover_pdfs(root: Path = SOURCE_ROOT) -> Iterator[tuple[str, Path]]:
    """루트 아래 1단계 카테고리 폴더의 PDF를 (folder_name, path)로 반환."""
    if not root.exists():
        return
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith(("."  , "__")):
            continue
        for pdf in entry.rglob("*.pdf"):
            yield entry.name, pdf


@dataclass
class FileEntry:
    path: str               # 절대경로 (key)
    category: str           # 폴더명 (e.g., "법률_UU")
    collection: str         # 컬렉션명 (e.g., "indonesia_uu")
    sha256: str
    size: int
    mtime: float
    chunk_ids: list[str] = field(default_factory=list)
    chunk_count: int = 0
    indexed_at: str = ""    # ISO 8601


class Manifest:
    """파일 → FileEntry 매핑. 디스크에 JSON으로 영속."""

    def __init__(self, path: Path = MANIFEST_PATH):
        self.path = path
        self.entries: dict[str, FileEntry] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return
        for k, v in (data or {}).items():
            try:
                self.entries[k] = FileEntry(**v)
            except TypeError:
                # 스키마 미스매치 → 무시 (다음 인덱싱 때 갱신됨)
                continue

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(
                {k: asdict(v) for k, v in self.entries.items()},
                f, ensure_ascii=False, indent=0
            )
        tmp.replace(self.path)

    def needs_index(self, path: Path, sha: str, size: int, mtime: float) -> bool:
        e = self.entries.get(str(path))
        if not e:
            return True
        return e.sha256 != sha or e.size != size

    def upsert(self, entry: FileEntry) -> None:
        self.entries[entry.path] = entry

    def delete(self, path: str) -> FileEntry | None:
        return self.entries.pop(path, None)

    def all_paths(self) -> set[str]:
        return set(self.entries.keys())


def hash_file(path: Path, chunk_size: int = 1 << 20) -> tuple[str, int, float]:
    """SHA-256, size, mtime 한 번에."""
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        while True:
            data = f.read(chunk_size)
            if not data:
                break
            h.update(data)
            size += len(data)
    return h.hexdigest(), size, path.stat().st_mtime


# ============= PDF 파싱 / 청킹 (워커 프로세스에서 호출) =============
# PyMuPDF(fitz) 1차 — pypdf 대비 4~8x 빠르고 텍스트 품질 동등/우위.
# pypdf는 PyMuPDF 실패 시 fallback. pdfplumber는 텍스트가 거의 안 나올 때만.
import pymupdf  # noqa: E402
import pypdf  # noqa: E402
import pdfplumber  # noqa: E402


CHUNK_SIZE = 700
CHUNK_OVERLAP = 120
MIN_TEXT_FALLBACK = 50  # 페이지당 텍스트가 이 이하면 pdfplumber 재시도
# 단일 PDF 크기 상한 (바이트). 이보다 큰 파일은 워커가 OOM/멈춤을 유발하므로
# 1차 패스에서는 건너뛴다 (별도 직렬 패스에서 처리). RAG_MAX_PDF_BYTES 환경변수로 override.
MAX_PDF_BYTES = int(os.getenv("RAG_MAX_PDF_BYTES", str(60 * 1024 * 1024)))  # 60MB
ARTICLE_PATTERN = re.compile(
    r"(Pasal\s+\d+[A-Za-z]?|Bab\s+[IVXLCDM]+|Pembukaan)",
    re.IGNORECASE,
)


def _detect_article(text: str) -> str:
    m = ARTICLE_PATTERN.search(text)
    return m.group(0).strip() if m else ""


def _chunk_text(text: str) -> Iterable[str]:
    text = (text or "").strip()
    if not text:
        return
    if len(text) <= CHUNK_SIZE:
        yield text
        return
    n = len(text)
    start = 0
    while start < n:
        end = min(start + CHUNK_SIZE, n)
        if end < n:
            window = text[start:end]
            split_at = max(window.rfind("\n"), window.rfind(". "), window.rfind("。"))
            if split_at > CHUNK_SIZE * 0.5:
                end = start + split_at + 1
        yield text[start:end].strip()
        if end >= n:
            break
        start = max(end - CHUNK_OVERLAP, start + 1)


def _extract_pages_pymupdf(pdf_path: Path) -> list[tuple[int, str]]:
    """PyMuPDF(fitz)로 페이지 추출. pypdf 대비 4~8x 빠름."""
    pages: list[tuple[int, str]] = []
    doc = pymupdf.open(str(pdf_path))
    try:
        for page_num, page in enumerate(doc, start=1):
            try:
                text = page.get_text() or ""
            except Exception:
                text = ""
            pages.append((page_num, text))
    finally:
        doc.close()
    return pages


def _extract_pages_pypdf(pdf_path: Path) -> list[tuple[int, str]]:
    """pypdf로 페이지 추출. PyMuPDF 실패 시 fallback."""
    pages: list[tuple[int, str]] = []
    reader = pypdf.PdfReader(str(pdf_path), strict=False)
    for page_num, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""
        pages.append((page_num, text))
    return pages


def _extract_pages_pdfplumber(pdf_path: Path) -> list[tuple[int, str]]:
    """pdfplumber로 정확한 추출. 느림."""
    pages: list[tuple[int, str]] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            pages.append((page_num, text))
    return pages


def parse_pdf(pdf_path: Path) -> list[dict]:
    """PDF → 청크. PyMuPDF 1차 (가장 빠름), 실패 시 pypdf, 텍스트가 거의 없으면 pdfplumber."""
    try:
        size = pdf_path.stat().st_size
    except OSError as exc:
        return [{"_error": f"stat:{type(exc).__name__}: {exc}"}]
    if size > MAX_PDF_BYTES:
        return [{"_error": f"oversize:{size} > MAX_PDF_BYTES={MAX_PDF_BYTES}"}]
    pages: list[tuple[int, str]] = []
    try:
        pages = _extract_pages_pymupdf(pdf_path)
    except Exception:
        try:
            pages = _extract_pages_pypdf(pdf_path)
        except Exception:
            try:
                pages = _extract_pages_pdfplumber(pdf_path)
            except Exception as exc2:
                return [{"_error": f"{type(exc2).__name__}: {exc2}"}]

    # 전체 텍스트가 너무 짧으면(≤MIN_TEXT_FALLBACK × 페이지수) pdfplumber로 한 번 더.
    # 환경변수 RAG_SKIP_PDFPLUMBER_FALLBACK=1로 비활성화 가능 (대량 인덱싱 시 속도 우선).
    total_chars = sum(len(t) for _, t in pages)
    if (pages and total_chars < MIN_TEXT_FALLBACK * len(pages)
            and not os.getenv("RAG_SKIP_PDFPLUMBER_FALLBACK")):
        try:
            pages = _extract_pages_pdfplumber(pdf_path)
        except Exception:
            pass  # 1차 결과 그대로 사용

    chunks: list[dict] = []
    for page_num, text in pages:
        text = re.sub(r"[ \t]+", " ", text or "")
        text = re.sub(r"\n{2,}", "\n", text).strip()
        if not text:
            continue
        for piece in _chunk_text(text):
            if not piece:
                continue
            chunks.append({
                "text": piece,
                "source": pdf_path.name,
                "page": page_num,
                "article": _detect_article(piece),
            })
    return chunks


def make_chunk_id(pdf_path: Path, page: int, idx: int) -> str:
    """파일 경로 해시(8자) + 페이지 + 인덱스로 안정 ID 생성.
    동일 파일 동일 위치는 동일 ID → upsert 가능.
    """
    h = hashlib.sha1(str(pdf_path).encode("utf-8")).hexdigest()[:10]
    return f"{h}-p{page}-{idx}"


def parse_pdf_worker(pdf_path_str: str) -> dict:
    """ProcessPoolExecutor용 wrapper. 직렬화 가능한 dict 반환.

    Windows의 cp949 콘솔 충돌을 피하기 위해 stdout/stderr를 강제 UTF-8화한다.
    의존성(pdfplumber/pdfminer 등)이 print/warning 시 cp949 인코딩 에러를 내며 워커가 죽는 것을 방지.
    """
    import sys, io
    try:
        if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if sys.stderr is not None and hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        # 콘솔이 없을 수도 있음 (백그라운드 실행). silently ignore.
        try:
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        except Exception:
            pass
    # pdfminer가 stderr로 경고를 쏘는 경우 대비: 로그 레벨 낮춤
    try:
        import logging
        logging.getLogger("pdfminer").setLevel(logging.ERROR)
        logging.getLogger("pypdf").setLevel(logging.ERROR)
    except Exception:
        pass

    pdf_path = Path(pdf_path_str)
    try:
        chunks = parse_pdf(pdf_path)
    except Exception as exc:
        chunks = [{"_error": f"{type(exc).__name__}: {exc}"}]
    return {
        "path": pdf_path_str,
        "chunks": chunks,
    }
