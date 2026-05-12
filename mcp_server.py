"""Indonesia Law RAG — MCP server (stdio).

Claude Code / Claude Desktop이 spawn하는 MCP 서버. 본인 컴퓨터에서 본인이
Claude.ai Pro/Max 구독으로 모델 호출하면서, 이 서버가 노출하는 retrieval
도구를 통해 D:\\rag_data\\chroma_db의 인도네시아 법령 청크를 검색한다.

원격 라이브 RAG(rag_server_v2 + cloudflared + Pages)와 병행 운영하는 목적:
  - 팀원들은 https://moon470an-sys.github.io/Indonesia-Law-Q-A/ (API key 결제)
  - 본인은 Claude Desktop/Code (Max 구독)에서 동일 corpus 질의

이 서버는 LLM을 호출하지 않는다. retrieval (vector search) 결과만 반환하고
모델 호출은 client(Claude.ai)가 한다. Anthropic Consumer ToS의 "ordinary,
individual usage" 범위 내 — Anthropic 공식 product인 Claude.ai 클라이언트가
사용자 인증으로 모델 호출하기 때문에 약관 위반 아님.

준비:
  .venv/Scripts/pip install mcp  (1.27+ 권장)

수동 테스트 (stdio echo):
  python mcp_server.py
  → Claude Desktop/Code가 호출할 stdio 프로토콜 대기. Ctrl+C로 종료.

Claude Desktop 등록 (Windows):
  파일: %APPDATA%\\Claude\\claude_desktop_config.json
  {
    "mcpServers": {
      "indonesia-law-rag": {
        "command": "C:\\\\Users\\\\yoonseok.moon\\\\OneDrive - ...\\\\.venv\\\\Scripts\\\\python.exe",
        "args": ["C:\\\\Users\\\\yoonseok.moon\\\\OneDrive - ...\\\\mcp_server.py"]
      }
    }
  }
  → Claude Desktop 재시작. 도구 아이콘에 indonesia-law-rag 등장하면 성공.

Claude Code 등록:
  claude mcp add indonesia-law-rag \\
    .venv/Scripts/python.exe mcp_server.py
  또는 ~/.claude.json (또는 .claude/mcp_servers.json) 수동 편집.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# stdio 프로토콜 channel 보호: 모든 진단 출력은 stderr로.
# (서드파티 라이브러리가 stdout으로 print하면 MCP가 깨짐)
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# rag_server_v2의 retrieval 함수 재사용. import 시 BGE-M3는 lazy load라
# cold start는 ~5초. 첫 search 호출 시 임베딩 모델 실제 로딩 (+30s 정도).
import rag_server_v2 as rag  # noqa: E402
from mcp.server.fastmcp import FastMCP  # noqa: E402

mcp = FastMCP("indonesia-law-rag")


CATEGORY_DESCRIPTIONS = {
    "constitution": "1945년 헌법 (UUD) 및 임시헌법(UUDS/Konstitusi RIS). 한 국가 최상위 법.",
    "uu": "법률 — Undang-Undang. 국회(DPR) 제정. 위계 3순위.",
    "pp": "정부령 — Peraturan Pemerintah. 법률 시행령. 위계 4순위.",
    "perpres": "대통령령 — Peraturan Presiden. 위계 5순위.",
    "permen": "장관령 — Peraturan Menteri. 상위법 위임/고유 권한.",
    "kepmen": "장관결정 — Keputusan Menteri.",
    "perda": "지방조례 — Peraturan Daerah (Provinsi/Kabupaten/Kota).",
    "lainnya": "기타 — Inpres(대통령지시), Surat Edaran 등.",
}


@mcp.tool()
def list_categories() -> str:
    """인도네시아 법령 RAG corpus의 카테고리 목록.

    각 카테고리는 별도 ChromaDB 컬렉션에 해당. search 도구의 `category`
    파라미터에 그대로 사용. 인덱싱된 청크 수도 함께 반환해서 어느 카테고리에
    자료가 있는지 한눈에 보이게.

    Returns:
        JSON {categories: [{key, name, chunks}, ...]}.
    """
    try:
        client = rag.get_chroma()
        out = []
        for key, desc in CATEGORY_DESCRIPTIONS.items():
            col_name = rag._CATEGORY_TO_COLLECTION.get(key)
            chunks = None
            if col_name:
                try:
                    col = client.get_collection(col_name)
                    chunks = col.count()
                except Exception:
                    chunks = None
            out.append({
                "key": key,
                "collection": col_name,
                "description": desc,
                "chunks": chunks,
            })
        return json.dumps({"categories": out}, ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps({"error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False)


@mcp.tool()
def search(category: str, query: str, top_k: int = 5) -> str:
    """카테고리 내 dense vector 검색 (BGE-M3 임베딩, 1024d).

    동작 순서:
      1. `query`(인도네시아어 또는 한국어 가능)를 BGE-M3로 임베딩
      2. 지정 `category`의 ChromaDB 컬렉션에서 코사인 유사도 top_k 청크
      3. 각 청크의 source 파일명, 페이지, 조항(article), 점수, 본문 일부 반환

    인용은 절대 합성하지 말 것 — 반드시 결과의 source/page/article 그대로 사용.
    조항 본문 전문이 필요하면 fetch_article 도구로 직접 조회.

    Args:
        category: list_categories의 key 값 (constitution, uu, pp, perpres,
                  permen, kepmen, perda, lainnya 중 하나).
        query: 검색 키워드 또는 자연어 질문 (한국어/인도네시아어 모두 가능,
               인도네시아어 핵심 키워드 포함 시 retrieval 정확도 ↑).
        top_k: 1~8 (기본 5). 8 초과는 8로 클램프.

    Returns:
        JSON {category, collection, count, results: [{source, page, article,
        score, text}, ...]}. error 발생 시 {error, results: []}.
    """
    result = rag.tool_search_collection(category=category, query=query, top_k=top_k)
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def fetch_article(source_file: str, pasal: str | None = None) -> str:
    """특정 PDF의 조항 본문 청크 직접 조회 (vector search 없음).

    동작:
      1. `source_file`의 파일명 prefix(UU_, PP_, Perpres_ 등)로 카테고리 추정
         → 단일 컬렉션만 scan (~0.3s, $and 필터 안 씀)
      2. ChromaDB에서 source 일치하는 청크를 가져온 뒤 Python 메모리에서
         `pasal`로 article 필터 (exact 또는 prefix 매칭, 예: "Pasal 7"은
         "Pasal 7"과 "Pasal 7A" 모두 매치)

    search 결과에서 발견한 흥미로운 source를 더 깊이 보고 싶을 때, 또는
    특정 Pasal의 정확한 원문을 인용해야 할 때 사용.

    Args:
        source_file: PDF 파일명 (확장자 포함). 예: "UU_12_Tahun_2011_Pembentukan_Peraturan_Perundang-undangan.pdf"
                     search 결과의 source 필드를 그대로 넣을 것.
        pasal: 조항 식별자. 예: "Pasal 7", "Pasal 1", "Pembukaan", "BAB II".
               None이면 source 전체 청크 중 일부 (최대 8개) 반환.

    Returns:
        JSON {source_file, pasal, category_resolved, count, results: [{id,
        source, page, article, text}, ...]}.
    """
    result = rag.tool_fetch_article_chunks(source_file=source_file, pasal=pasal)
    return json.dumps(result, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    # stdio transport. Claude Desktop/Code가 child process로 spawn하면
    # stdin/stdout으로 JSON-RPC 메시지 교환.
    mcp.run(transport="stdio")
