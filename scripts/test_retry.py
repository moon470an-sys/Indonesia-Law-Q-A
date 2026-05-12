"""Phase 5b verify_citations + should_retry + build_retry_user_message unit 검증."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import rag_server_v2 as r


def case(label, expected, actual):
    ok = expected == actual
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}: expected={expected!r} actual={actual!r}")
    return ok


def chunk(source, page, article, text="..."):
    return {"metadata": {"source": source, "page": page, "article": article}, "text": text}


print("\n###### verify_citations ######")
retrieved = [
    chunk("UU_5_Tahun_1967.pdf", 3, "Pasal 3"),
    chunk("UU_5_Tahun_1967.pdf", 5, "Pasal 6"),
    chunk("PP_19_Tahun_2024.pdf", 12, "Pasal 1"),
]

# (1) 완벽 매치
ans1 = "산림법 정의 (Pasal 3, 출처: UU_5_Tahun_1967.pdf, p.3) 참고."
v1 = r.verify_citations(ans1, retrieved)
case("정확 매치 total/verified/unverified", (1, 1, 0), (v1["total"], v1["verified"], v1["unverified"]))

# (2) article ok, page mismatch
ans2 = "(Pasal 3, 출처: UU_5_Tahun_1967.pdf, p.7)"
v2 = r.verify_citations(ans2, retrieved)
case("page mismatch unverified=1", 1, v2["unverified"])
case("page mismatch status", "page_mismatch", v2["items"][0]["status"])

# (3) source not in retrieved
ans3 = "(Pasal 7, 출처: UU_99_Fake.pdf, p.1)"
v3 = r.verify_citations(ans3, retrieved)
case("source missing", "source_not_in_context", v3["items"][0]["reason"])

# (4) article mismatch
ans4 = "(Pasal 99, 출처: UU_5_Tahun_1967.pdf, p.3)"
v4 = r.verify_citations(ans4, retrieved)
case("article mismatch", "article_mismatch", v4["items"][0]["status"])

# (5) ayat 포함 인용
ans5 = "(Pasal 6 ayat (1), 출처: UU_5_Tahun_1967.pdf, p.5) ..."
v5 = r.verify_citations(ans5, retrieved)
case("ayat 포함 verified", 1, v5["verified"])

# (6) 다중 인용
ans6 = (
    "산림 (Pasal 3, 출처: UU_5_Tahun_1967.pdf, p.3) 및 "
    "산림법 (Pasal 6, 출처: UU_5_Tahun_1967.pdf, p.5) 와 "
    "조항 (Pasal 1, 출처: PP_19_Tahun_2024.pdf, p.12)"
)
v6 = r.verify_citations(ans6, retrieved)
case("다중 인용 total=3 verified=3", (3, 3), (v6["total"], v6["verified"]))


print("\n###### should_retry ######")
# critique unknown + no verifier → no retry
case("unknown+no verifier", (False, "ok"), r.should_retry({"confidence": "unknown", "issues": []}))

# verifier unverified > 0 → retry (LLM critique이 high여도)
case("verifier unverified", (True, "verifier_unverified=2"),
     r.should_retry({"confidence": "high", "issues": []}, {"unverified": 2}))

# critique low → retry
case("low confidence", (True, "confidence=low"),
     r.should_retry({"confidence": "low", "issues": []}, {"unverified": 0}))

# hallucination → retry
case("hallucination issue", (True, "serious_issues=1"),
     r.should_retry({"confidence": "medium",
                     "issues": [{"type": "hallucination", "description": "..."}]},
                    {"unverified": 0}))

# medium + 2 missing → retry (RETRY_ON_MEDIUM_MIN_ISSUES=2 기본)
case("medium+2 missing", (True, "medium+issues=2"),
     r.should_retry({"confidence": "medium",
                     "issues": [{"type": "missing", "description": "..."},
                                {"type": "missing", "description": "..."}]},
                    {"unverified": 0}))

# high + 1 missing → no retry
case("high+1 missing", (False, "ok"),
     r.should_retry({"confidence": "high",
                     "issues": [{"type": "missing", "description": "..."}]},
                    {"unverified": 0}))


print("\n###### build_retry_user_message ######")
msg = r.build_retry_user_message(
    question="산림법은?",
    context="[참고문서1] ...",
    prior_answer="(Pasal 3, 출처: UU_5_Tahun_1967.pdf, p.3) 4가지 분류...",
    critique={
        "confidence": "medium",
        "summary": "부분 미흡",
        "issues": [{"type": "hallucination", "description": "Pasal 3에 4분류 없음"}],
    },
    verifier={"items": [{"raw": "(Pasal 3, 출처: UU_5_Tahun_1967.pdf, p.7)",
                         "status": "page_mismatch", "reason": "page 7 not in chunks"}]},
)
# critique issue + verifier issue 둘 다 포함되어야 함
case("retry msg에 hallucination 포함", True, "hallucination" in msg)
case("retry msg에 verifier 인용 raw 포함", True, "(Pasal 3, 출처: UU_5_Tahun_1967.pdf, p.7)" in msg)
case("retry msg에 question 포함", True, "산림법은?" in msg)
case("retry msg에 prior_answer 포함", True, "4가지 분류" in msg)

print("\n###### done ######")
