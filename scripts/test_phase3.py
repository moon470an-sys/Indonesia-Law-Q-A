"""Phase 3 — intent classification + sub-query + multi-paragraph HyDE 검증."""
import json
import time
import urllib.request


def test_stream(question: str, label: str):
    print(f"\n{'='*70}\n{label}\nQ: {question}\n{'='*70}")
    payload = json.dumps({"question": question}).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:8000/query/stream",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    first_token_t = None
    intent_info = None
    sources_count = 0
    answer_len = 0
    debug = None

    with urllib.request.urlopen(req, timeout=240) as resp:
        event = None
        for raw_line in resp:
            line = raw_line.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")
            if not line:
                event = None
                continue
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_str = line[len("data:"):].strip()
                try:
                    data = json.loads(data_str)
                except Exception:
                    continue
                now = time.time() - t0
                if event == "intent":
                    intent_info = data
                    print(f"  [{now:.2f}s] INTENT: {data}")
                elif event == "sources":
                    sources_count = len(data.get("sources", []))
                    print(f"  [{now:.2f}s] sources={sources_count}")
                elif event == "token":
                    if first_token_t is None:
                        first_token_t = now
                    answer_len += len(data.get("text", ""))
                elif event == "done":
                    debug = data.get("debug")
                elif event == "error":
                    print(f"  ERROR: {data}")
                    return

    total = time.time() - t0
    print(f"\n  TIMING — first_token: {first_token_t:.2f}s, total: {total:.2f}s, answer_len: {answer_len}")
    if debug:
        an = debug.get("analysis", {})
        st = debug.get("strategy", {})
        us = debug.get("usage", {})
        print(f"  ANALYSIS — intent={an.get('intent')}, sub_queries={len(an.get('sub_queries',[]))}, "
              f"keywords={len(an.get('id_keywords',[]))}, hyde={an.get('hypothetical_id_answers_count')}, "
              f"cat_filter={an.get('category_filter')}")
        print(f"  STRATEGY — {st}")
        print(f"  RETRIEVE — num_queries={debug.get('num_queries')}, unique={debug.get('candidates_unique')}, topN={debug.get('candidates_topN')}")
        print(f"  USAGE — cache_create={us.get('cache_creation_input_tokens')}, cache_read={us.get('cache_read_input_tokens')}, input={us.get('input_tokens')}, output={us.get('output_tokens')}")


if __name__ == "__main__":
    tests = [
        ("인도네시아 헌법의 공식 명칭은?", "[A] 단답형 → single_answer"),
        ("UU 12/2011과 UU 10/2004의 차이를 비교해줘", "[B] 비교 → comparison"),
        ("환경 오염 사고가 발생하면 어떤 법령이 적용되나요?", "[C] 사례 → case_application"),
    ]
    for q, label in tests:
        test_stream(q, label)
        time.sleep(2)
