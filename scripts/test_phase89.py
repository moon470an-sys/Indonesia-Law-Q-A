"""Phase 8 (caching) + Phase 9 (fetch latency) 검증."""
import json
import time
import urllib.request


def call_stream(question: str, label: str) -> dict:
    print(f"\n{'='*70}\n{label}\nQ: {question}\n{'='*70}")
    payload = json.dumps({"question": question}).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:8000/query/stream",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    debug = None
    tool_calls = []
    first_token_t = None
    answer_len = 0

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
                if event == "tool_result":
                    tool_calls.append(data)
                elif event == "token":
                    if first_token_t is None:
                        first_token_t = time.time() - t0
                    answer_len += len(data.get("text", ""))
                elif event == "done":
                    debug = data.get("debug")

    total = time.time() - t0
    print(f"  total={total:.1f}s, first_token={first_token_t:.1f}s, answer={answer_len}자")

    if debug:
        u = debug.get("usage", {})
        print(f"  CACHE - create={u.get('cache_creation_input_tokens')}, "
              f"read={u.get('cache_read_input_tokens')}, "
              f"input={u.get('input_tokens')}, output={u.get('output_tokens')}")
        ag = debug.get("agent", {})
        print(f"  AGENT - iters={ag.get('iterations')}, tool_calls={ag.get('tool_call_count')}")
        for tc in ag.get("tool_calls", []):
            print(f"    - {tc['name']}: {tc.get('elapsed_sec')}s, count={tc.get('result_count')}")
    return {"debug": debug, "tool_calls": tool_calls}


if __name__ == "__main__":
    # 1) Phase 8: 같은 system+tools로 2회 호출 → 2회차 cache_read 발생 기대
    print("\n###### Phase 8: prompt caching 검증 ######")
    call_stream("법령 위계 1단어로", "[Cache-1] 첫 호출")
    time.sleep(3)
    call_stream("정부령 1단어로", "[Cache-2] 둘째 호출 — cache_read 기대")

    # 2) Phase 9: fetch_article_chunks latency
    print("\n###### Phase 9: fetch latency 검증 ######")
    call_stream(
        "UU 12 Tahun 2011 Pembentukan Peraturan Perundang-undangan의 Pasal 7 본문을 fetch_article_chunks 도구로 정확히 인용해줘",
        "[Fetch-1] fetch_article_chunks 호출 유도",
    )
