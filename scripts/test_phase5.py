"""Phase 5: critique SSE event 수신 검증."""
import json
import time
import urllib.request


def test(question: str, label: str):
    print(f"\n{'='*70}\n{label}\nQ: {question}\n{'='*70}")
    payload = json.dumps({"question": question}).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:8000/query/stream",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    critique = None
    answer_len = 0
    first_token_t = None
    debug = None

    with urllib.request.urlopen(req, timeout=300) as resp:
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
                    print(f"  [{now:.2f}s] intent={data.get('intent')}")
                elif event == "sources":
                    print(f"  [{now:.2f}s] sources={len(data.get('sources',[]))}")
                elif event == "token":
                    if first_token_t is None:
                        first_token_t = now
                    answer_len += len(data.get("text", ""))
                elif event == "critique":
                    critique = data
                    print(f"  [{now:.2f}s] CRITIQUE received")
                elif event == "done":
                    debug = data.get("debug")

    total = time.time() - t0
    print(f"\n  TIMING — first_token={first_token_t:.2f}s, total={total:.2f}s, answer={answer_len}자")
    if critique:
        print(f"\n  CRITIQUE:")
        print(f"    confidence: {critique.get('confidence')}")
        print(f"    verified_citations_count: {critique.get('verified_citations_count')}")
        print(f"    summary: {critique.get('summary')}")
        issues = critique.get("issues", [])
        print(f"    issues ({len(issues)}):")
        for i in issues[:5]:
            print(f"      - [{i.get('type','?')}] {i.get('description','')[:120]}")
    if debug:
        tm = debug.get("timing", {})
        print(f"\n  SERVER TIMING — retrieve={tm.get('retrieve_sec')}s, rerank={tm.get('rerank_sec')}s, "
              f"generate={tm.get('generate_sec')}s, critique={tm.get('critique_sec')}s, total={tm.get('total_sec')}s")


if __name__ == "__main__":
    tests = [
        ("법령 위계와 헌법의 위치를 표로 정리해줘", "[A] hierarchy"),
    ]
    for q, label in tests:
        test(q, label)
        time.sleep(2)
