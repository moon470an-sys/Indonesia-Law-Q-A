"""Phase 6: Agentic tool_use 검증. tool_call / tool_result event 흐름."""
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
    tool_calls = []
    tool_results = []
    first_token_t = None
    answer_len = 0
    debug = None
    critique = None

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
                elif event == "tool_call_start":
                    print(f"  [{now:.2f}s] >>> tool_call_start: {data.get('name')} (iter {data.get('iteration')})")
                elif event == "tool_call":
                    tool_calls.append(data)
                    inp = data.get("input", {})
                    inp_summary = ", ".join(f"{k}={str(v)[:50]}" for k, v in inp.items())
                    print(f"  [{now:.2f}s] >>> tool_call: {data.get('name')}({inp_summary})")
                elif event == "tool_result":
                    tool_results.append(data)
                    print(f"  [{now:.2f}s] <<< tool_result: {data.get('name')} count={data.get('result_count')} ({data.get('elapsed_sec')}s)")
                elif event == "token":
                    if first_token_t is None:
                        first_token_t = now
                    answer_len += len(data.get("text", ""))
                elif event == "critique":
                    critique = data
                elif event == "done":
                    debug = data.get("debug")

    total = time.time() - t0
    print(f"\n  TIMING — first_token={first_token_t:.2f}s, total={total:.2f}s, answer={answer_len}자")
    print(f"  TOOL CALLS — {len(tool_calls)} requested, {len(tool_results)} executed")
    if critique:
        print(f"  CRITIQUE — confidence={critique.get('confidence')}, citations={critique.get('verified_citations_count')}, issues={len(critique.get('issues',[]))}")
    if debug and debug.get("agent"):
        ag = debug["agent"]
        print(f"  AGENT — iterations={ag.get('iterations')}, tool_call_count={ag.get('tool_call_count')}")
        for tc in ag.get("tool_calls", []):
            print(f"    - {tc['name']}: result_count={tc.get('result_count')}, size={tc.get('result_size')}, {tc.get('elapsed_sec')}s")


if __name__ == "__main__":
    tests = [
        # tool_use 유도 — 답변 도중 추가 검색 자연스럽게 호출
        ("UU 12 Tahun 2011의 Pasal 7 본문을 정확히 인용해서 알려줘",
         "[A] fetch_article_chunks 호출 유도"),
        ("환경 보호 분야에서 정부령(PP)에 어떤 규정이 있는지 추가로 찾아봐",
         "[B] search_collection 호출 유도"),
    ]
    for q, label in tests:
        test(q, label)
        time.sleep(2)
