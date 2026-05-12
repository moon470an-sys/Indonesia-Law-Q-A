"""/query/stream SSE 테스트 클라이언트. 첫 토큰 latency + cache 사용 측정."""
import json
import sys
import time
import urllib.request


def test_stream(question: str, label: str):
    print(f"\n=== {label} ===")
    payload = json.dumps({"question": question}).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:8000/query/stream",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    first_token_t = None
    sources_t = None
    token_count = 0
    answer = []
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
                if event == "sources":
                    sources_t = now
                    print(f"  [{now:.2f}s] sources received ({len(data.get('sources', []))} items)")
                elif event == "token":
                    if first_token_t is None:
                        first_token_t = now
                        print(f"  [{now:.2f}s] FIRST TOKEN: {data.get('text', '')[:50]!r}")
                    token_count += 1
                    answer.append(data.get("text", ""))
                elif event == "done":
                    debug = data.get("debug")
                    print(f"  [{now:.2f}s] done, tokens={token_count}")
                elif event == "error":
                    print(f"  [{now:.2f}s] ERROR: {data}")

    total = time.time() - t0
    print(f"\n  TIMING — sources: {sources_t:.2f}s, first_token: {first_token_t:.2f}s, total: {total:.2f}s")
    if debug:
        usage = debug.get("usage", {})
        print(f"  USAGE — input={usage.get('input_tokens')}, output={usage.get('output_tokens')}, "
              f"cache_create={usage.get('cache_creation_input_tokens')}, "
              f"cache_read={usage.get('cache_read_input_tokens')}")
        print(f"  TIMING (server) — {debug.get('timing')}")
    print(f"\n  ANSWER preview ({len(''.join(answer))} chars):")
    print("  " + "".join(answer)[:300].replace("\n", "\n  "))
    return debug


if __name__ == "__main__":
    q1 = "인도네시아 헌법 제25조 내용을 알려줘"
    d1 = test_stream(q1, "1차 호출 (cache_creation 예상)")
    time.sleep(2)
    d2 = test_stream(q1, "2차 호출 (cache_read 예상, latency 단축)")
