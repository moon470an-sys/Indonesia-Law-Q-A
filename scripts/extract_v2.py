"""
PyMuPDF 기반 새 추출/chunking 모듈 (Plan C+).

기존 ingest.py / embed_worker.py 의 pdfplumber 경로와 격리.
새 컬렉션 (v2_indonesia_*) 인덱싱 전용.

핵심 개선:
- PyMuPDF: pdfplumber보다 더 정확한 텍스트 추출 (특히 CID/embedded font PDF)
- NFKC normalize + soft hyphen / non-breaking space 정리
- 페이지 경계 머지 (단어 중간 잘림 보정)
- 의미 단위 chunking: Pasal/BAB boundary 우선
- 노이즈 청크 필터: 서명/표지/페이지번호만 있는 짧은 청크 제거
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import fitz  # PyMuPDF


# ===== 텍스트 정리 =====

# pdfplumber/PyMuPDF 추출 시 자주 끼는 노이즈 문자
_NOISE_CHARS = {
    " ": " ",   # non-breaking space → space
    "­": "",    # soft hyphen → 제거
    "​": "",    # zero-width space
    "‌": "",    # zero-width non-joiner
    "‍": "",    # zero-width joiner
    "﻿": "",    # BOM
    "\xa0": " ",     # alias of
}

_RE_MULTI_SPACE = re.compile(r"[ \t]+")
_RE_MULTI_NEWLINE = re.compile(r"\n{3,}")
_RE_TRAILING_SPACE = re.compile(r"[ \t]+\n")
_RE_LEADING_SPACE = re.compile(r"\n[ \t]+")
# PDF 끝 페이지 번호 패턴: " - 8 - " 또는 "Halaman 8 dari 10" 등
_RE_PAGE_FOOTER = re.compile(
    r"^[\s\-]*\b(?:Halaman|Hal\.|Page|Hal)\s+\d+\s+(?:dari|of|/)\s+\d+[\s\-]*$"
    r"|^\s*-\s*\d+\s*-\s*$",
    re.MULTILINE,
)
# 단독 페이지 번호만 있는 줄
_RE_ISOLATED_PAGENUM = re.compile(r"^\s*\d{1,4}\s*$", re.MULTILINE)


def clean_text(text: str) -> str:
    """Unicode normalize + 노이즈 문자 정리 + 공백 정규화."""
    if not text:
        return ""

    # NFKC: '·' 같은 특수 문자, 호환 디스플레이 문자를 표준화
    text = unicodedata.normalize("NFKC", text)

    # 잘 알려진 노이즈 char 치환
    for src, dst in _NOISE_CHARS.items():
        if src in text:
            text = text.replace(src, dst)

    # 페이지 footer / 단독 페이지 번호 제거
    text = _RE_PAGE_FOOTER.sub("", text)
    text = _RE_ISOLATED_PAGENUM.sub("", text)

    # 공백 정규화
    text = _RE_TRAILING_SPACE.sub("\n", text)
    text = _RE_LEADING_SPACE.sub("\n", text)
    text = _RE_MULTI_SPACE.sub(" ", text)
    text = _RE_MULTI_NEWLINE.sub("\n\n", text)

    return text.strip()


# ===== 페이지 경계 머지 =====


def merge_page_boundaries(pages: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """페이지 마지막 줄이 미완 문장(. ! ? 으로 끝나지 않음)이면
    다음 페이지 첫 줄과 합쳐서 단어 중간 잘림 보정.

    Returns:
        머지된 pages — 각 요소가 (대표_page_num, text). 머지된 경우 page_num은 시작 페이지.
    """
    if not pages:
        return []

    merged: list[tuple[int, str]] = []
    buffer_page = pages[0][0]
    buffer_text = pages[0][1]

    for next_page, next_text in pages[1:]:
        # 현재 buffer가 미완으로 끝나는지 검사
        last_char = buffer_text.rstrip()[-1:] if buffer_text.rstrip() else ""
        ends_open = last_char and last_char not in ".!?…。:;)\"”’"
        # 다음 페이지가 소문자/연속 영문자로 시작하면 단어 잘림 거의 확정
        first_word = next_text.lstrip()[:30]
        starts_lower = first_word and (first_word[0].islower() or first_word[0].isdigit())

        if ends_open and starts_lower:
            # 단어 잘림 보정: 단순 concat (공백 하나로 연결)
            buffer_text = buffer_text.rstrip() + " " + next_text.lstrip()
        else:
            merged.append((buffer_page, buffer_text))
            buffer_page = next_page
            buffer_text = next_text

    merged.append((buffer_page, buffer_text))
    return merged


# ===== Article / Section 인식 =====

# 인도네시아 법령 조항/장 패턴
_RE_PASAL = re.compile(r"\bPasal\s+(\d+[A-Za-z]?)\b", re.IGNORECASE)
_RE_BAB = re.compile(r"\bBAB\s+([IVXLCDM]+)\b", re.IGNORECASE)
_RE_BAGIAN = re.compile(r"\bBagian\s+(\w+)\b", re.IGNORECASE)
_RE_PEMBUKAAN = re.compile(r"\bPembukaan\b", re.IGNORECASE)


def detect_article(text: str) -> str:
    """텍스트에서 가장 먼저 등장하는 조항/장 표시 추출."""
    for pat, label in [
        (_RE_PEMBUKAAN, "Pembukaan"),
        (_RE_BAB, None),
        (_RE_PASAL, None),
        (_RE_BAGIAN, None),
    ]:
        m = pat.search(text)
        if m:
            if label:
                return label
            return m.group(0).strip()
    return ""


# ===== 의미 단위 chunking =====

# 우선순위: Pasal > BAB > 단락(빈 줄) > 문장
_RE_ARTICLE_BOUNDARY = re.compile(
    r"(?=\n\s*(?:BAB\s+[IVXLCDM]+|Pasal\s+\d+[A-Za-z]?|Bagian\s+\w+|Pembukaan)\b)",
    re.IGNORECASE,
)

CHUNK_TARGET_SIZE = 1200       # 목표 chunk 길이 (문자)
CHUNK_MAX_SIZE = 1800          # 최대 (이거 넘으면 무조건 강제 분할)
CHUNK_MIN_SIZE = 80            # 이보다 짧으면 노이즈 가능성 (인접 chunk와 머지)
CHUNK_OVERLAP = 180            # chunk 간 overlap (문자)


def _split_by_articles(text: str) -> list[str]:
    """조항 boundary로 1차 split. 매치 안 되면 [text] 그대로 반환."""
    parts = _RE_ARTICLE_BOUNDARY.split(text)
    parts = [p.strip() for p in parts if p.strip()]
    return parts if parts else [text]


def _split_by_paragraphs(text: str) -> list[str]:
    """빈 줄 기준 단락 split."""
    parts = re.split(r"\n\s*\n", text)
    return [p.strip() for p in parts if p.strip()]


def _split_by_sentences(text: str) -> list[str]:
    """문장 단위 split (인니어/한국어). 단순 . ! ? 기준."""
    parts = re.split(r"(?<=[.!?。])\s+", text)
    return [p.strip() for p in parts if p.strip()]


def _merge_small_chunks(parts: list[str], target: int, max_size: int) -> list[str]:
    """작은 조각들을 target 근처로 묶음."""
    merged: list[str] = []
    buf = ""
    for p in parts:
        if not buf:
            buf = p
            continue
        if len(buf) + len(p) + 1 <= max_size:
            buf = buf + "\n" + p
            if len(buf) >= target:
                merged.append(buf)
                buf = ""
        else:
            merged.append(buf)
            buf = p
    if buf:
        merged.append(buf)
    return merged


def _force_split(text: str, target: int, max_size: int, overlap: int) -> list[str]:
    """target보다 큰 단일 단락을 target 근처로 강제 분할. 문장 경계 우선."""
    if len(text) <= max_size:
        return [text]

    chunks: list[str] = []
    sentences = _split_by_sentences(text)
    buf = ""
    for s in sentences:
        if not buf:
            buf = s
            continue
        if len(buf) + len(s) + 1 <= target:
            buf = buf + " " + s
        else:
            chunks.append(buf)
            # overlap: 이전 buf 끝부분 overlap만큼 가져와서 새 buf 시작
            if overlap > 0 and len(buf) > overlap:
                buf = buf[-overlap:] + " " + s
            else:
                buf = s
    if buf:
        chunks.append(buf)

    # 그래도 max_size 넘는 게 있으면 글자 단위로 잘림
    final: list[str] = []
    for c in chunks:
        if len(c) <= max_size:
            final.append(c)
        else:
            for i in range(0, len(c), target):
                final.append(c[i:i + target])
    return final


def chunk_text_v2(text: str) -> list[str]:
    """의미 단위 chunking.

    1. Article boundary (Pasal/BAB)로 1차 split
    2. 각 part가 target보다 크면 단락 split → 문장 split
    3. 작은 part들은 인접 part와 머지
    4. 너무 큰 단일 part는 강제 분할 (overlap 포함)
    """
    text = text.strip()
    if not text:
        return []

    parts = _split_by_articles(text)
    expanded: list[str] = []
    for p in parts:
        if len(p) <= CHUNK_MAX_SIZE:
            expanded.append(p)
        else:
            # 단락 split → 큰 단락은 문장 split → 그래도 크면 강제
            paras = _split_by_paragraphs(p)
            for para in paras:
                if len(para) <= CHUNK_MAX_SIZE:
                    expanded.append(para)
                else:
                    expanded.extend(_force_split(para, CHUNK_TARGET_SIZE, CHUNK_MAX_SIZE, CHUNK_OVERLAP))

    # 작은 조각 머지
    merged = _merge_small_chunks(expanded, CHUNK_TARGET_SIZE, CHUNK_MAX_SIZE)

    # 너무 짧은 chunk 필터 (서명/페이지번호 잔여물)
    filtered = [c for c in merged if len(c) >= CHUNK_MIN_SIZE]

    return filtered


# ===== 노이즈 청크 추가 필터 =====

# 청크 내용이 거의 metadata/서명/표지만이면 제외
_RE_SIGNATURE_BLOCK = re.compile(
    r"^(?:PRESIDEN|MENTERI|GUBERNUR|WALIKOTA|BUPATI|REPUBLIK INDONESIA|"
    r"DEWAN PERWAKILAN|TTD|SALINAN|Diundangkan|Ditetapkan|LEMBARAN NEGARA|"
    r"BERITA NEGARA|SOEMARDI|SARTONO)",
    re.IGNORECASE | re.MULTILINE,
)


def is_noise_chunk(text: str) -> bool:
    """서명란/페이지번호/표지만 있는 청크 판정."""
    t = text.strip()
    if len(t) < CHUNK_MIN_SIZE:
        return True

    # 알파벳/숫자 비율이 너무 낮으면 (특수문자 도배) 노이즈
    alnum_count = sum(1 for c in t if c.isalnum())
    if alnum_count / max(len(t), 1) < 0.3:
        return True

    # 거의 모든 줄이 signature block 패턴
    lines = [l.strip() for l in t.split("\n") if l.strip()]
    if not lines:
        return True
    sig_lines = sum(1 for l in lines if _RE_SIGNATURE_BLOCK.search(l))
    if sig_lines / len(lines) > 0.6 and len(t) < 400:
        return True

    return False


# ===== PDF 추출 =====


@dataclass
class ExtractedChunk:
    text: str
    source: str       # PDF 파일명
    page: int         # 청크 시작 페이지
    article: str      # 인식된 조항/장
    category: str     # 폴더 카테고리 (헌법/UU/...)


def extract_pdf_v2(pdf_path: Path, category: str = "") -> list[ExtractedChunk]:
    """PyMuPDF + 정리 + 페이지 머지 + 의미 단위 chunking."""
    doc = fitz.open(str(pdf_path))
    try:
        # 1) 페이지별 raw text
        raw_pages: list[tuple[int, str]] = []
        for idx, page in enumerate(doc, start=1):
            t = page.get_text("text") or ""
            t = clean_text(t)
            if t:
                raw_pages.append((idx, t))
    finally:
        doc.close()

    if not raw_pages:
        return []

    # 2) 페이지 경계 머지
    merged_pages = merge_page_boundaries(raw_pages)

    # 3) 페이지별로 chunking
    chunks: list[ExtractedChunk] = []
    source = pdf_path.name
    for page_num, page_text in merged_pages:
        for piece in chunk_text_v2(page_text):
            if is_noise_chunk(piece):
                continue
            chunks.append(ExtractedChunk(
                text=piece,
                source=source,
                page=page_num,
                article=detect_article(piece),
                category=category,
            ))

    return chunks


# ===== quality 비교용 =====


def compare_with_legacy(pdf_path: Path) -> dict:
    """기존 pdfplumber 결과 vs 새 PyMuPDF 결과를 quality 지표로 비교."""
    import pdfplumber

    # legacy
    legacy_pages: list[tuple[int, str]] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for idx, page in enumerate(pdf.pages, start=1):
            t = page.extract_text() or ""
            t = re.sub(r"[ \t]+", " ", t)
            t = re.sub(r"\n{2,}", "\n", t).strip()
            if t:
                legacy_pages.append((idx, t))
    legacy_full = "\n".join(t for _, t in legacy_pages)
    legacy_noise_chars = sum(legacy_full.count(c) for c in _NOISE_CHARS if c != "\xa0")

    # new
    new_chunks = extract_pdf_v2(pdf_path)
    new_full = "\n".join(c.text for c in new_chunks)
    new_noise_chars = sum(new_full.count(c) for c in _NOISE_CHARS if c != "\xa0")

    return {
        "pdf": pdf_path.name,
        "legacy": {
            "pages": len(legacy_pages),
            "total_chars": len(legacy_full),
            "noise_chars": legacy_noise_chars,
            "first_chunk_sample": legacy_pages[0][1][:300] if legacy_pages else "",
        },
        "new": {
            "pages": len(set(c.page for c in new_chunks)),
            "chunks": len(new_chunks),
            "total_chars": len(new_full),
            "noise_chars": new_noise_chars,
            "avg_chunk_size": int(len(new_full) / len(new_chunks)) if new_chunks else 0,
            "with_article": sum(1 for c in new_chunks if c.article),
            "first_chunk_sample": new_chunks[0].text[:300] if new_chunks else "",
        },
    }


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) < 2:
        print("usage: python extract_v2.py <pdf-path> [<pdf-path> ...]")
        sys.exit(1)

    for arg in sys.argv[1:]:
        path = Path(arg)
        if not path.exists():
            print(f"SKIP missing: {arg}")
            continue
        print(f"\n========== {path.name} ==========")
        result = compare_with_legacy(path)
        print(json.dumps(result, ensure_ascii=False, indent=2))
