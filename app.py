"""
Streamlit UI - 인도네시아 헌법 RAG 테스트.

실행:
    streamlit run app.py
"""
from __future__ import annotations

import os
from pathlib import Path

import requests
import streamlit as st
from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(PROJECT_DIR / ".env")

API_URL = os.getenv("RAG_API_URL", "http://127.0.0.1:8000")

st.set_page_config(
    page_title="인도네시아 헌법 RAG",
    page_icon="📜",
    layout="wide",
)

st.title("📜 인도네시아 헌법 RAG 테스트")
st.caption(
    "인도네시아 헌법 원문(UUD 1945, 개정 1~4차, RIS 1949, UUDS 1950)을 근거로 "
    "Claude가 한국어로 답변합니다."
)

with st.sidebar:
    st.header("설정")
    api_url = st.text_input("RAG API URL", value=API_URL)
    top_k = st.slider("검색할 청크 수 (Top-K)", min_value=1, max_value=15, value=5)

    st.markdown("---")
    st.subheader("서버 상태")
    if st.button("Health 확인"):
        try:
            r = requests.get(f"{api_url}/health", timeout=10)
            st.json(r.json())
        except Exception as exc:
            st.error(f"연결 실패: {exc}")

    st.markdown("---")
    st.subheader("예시 질문")
    examples = [
        "인도네시아 헌법에서 대통령의 권한은?",
        "인도네시아 국민의 기본권은 무엇인가?",
        "인도네시아 의회 구성은 어떻게 되어 있나?",
    ]
    for ex in examples:
        if st.button(ex, use_container_width=True):
            st.session_state["pending_question"] = ex

if "history" not in st.session_state:
    st.session_state["history"] = []

default_q = st.session_state.pop("pending_question", "")
question = st.text_area(
    "질문을 입력하세요",
    value=default_q,
    height=100,
    placeholder="예) 인도네시아 헌법에서 대통령의 권한은?",
)

col1, col2 = st.columns([1, 5])
with col1:
    submit = st.button("질문하기", type="primary", use_container_width=True)
with col2:
    if st.button("대화 초기화"):
        st.session_state["history"] = []
        st.rerun()

if submit and question.strip():
    with st.spinner("Claude가 헌법 문서를 검토하는 중..."):
        try:
            resp = requests.post(
                f"{api_url}/query",
                json={"question": question.strip(), "top_k": top_k},
                timeout=120,
            )
            resp.raise_for_status()
            data = resp.json()
            st.session_state["history"].insert(
                0,
                {"q": question.strip(), "a": data["answer"], "sources": data["sources"]},
            )
        except Exception as exc:
            st.error(f"요청 실패: {exc}")

for idx, item in enumerate(st.session_state["history"]):
    st.markdown("---")
    st.markdown(f"### Q. {item['q']}")
    st.markdown(item["a"])

    with st.expander(f"🔎 검색된 출처 {len(item['sources'])}건 보기", expanded=(idx == 0)):
        for i, src in enumerate(item["sources"], start=1):
            article = src.get("article") or "조항 미확인"
            score = src.get("score", 0.0)
            st.markdown(
                f"**[{i}] {src['source']} · p.{src['page']} · {article}** · 유사도 {score:.3f}"
            )
            st.code(src["snippet"], language="text")
