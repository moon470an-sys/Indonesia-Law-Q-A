"""Phase 4: reranker reasoning + diversity penalty 검증."""
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
    sources = []
    first_token_t = None
    answer_len = 0
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
                    print(f"  [{now:.2f}s] intent={data.get('intent')} top_k={data.get('top_k')}")
                elif event == "sources":
                    sources = data.get("sources", [])
                    print(f"  [{now:.2f}s] sources={len(sources)}")
                elif event == "token":
                    if first_token_t is None:
                        first_token_t = now
                    answer_len += len(data.get("text", ""))
                elif event == "done":
                    debug = data.get("debug")

    total = time.time() - t0
    print(f"\n  TIMING — first_token={first_token_t:.2f}s, total={total:.2f}s, answer_len={answer_len}")

    # source 다양성 분석
    src_count: dict[str, int] = {}
    for s in sources:
        src_count[s["source"]] = src_count.get(s["source"], 0) + 1
    duplicates = {k: v for k, v in src_count.items() if v > 1}
    print(f"\n  DIVERSITY — 고유 출처 {len(src_count)}/{len(sources)}, 중복 출처: {duplicates if duplicates else '없음'}")

    # llm_score, llm_reason 표시
    print(f"\n  TOP 5 sources (llm_score / diversity_mul):")
    for i, s in enumerate(sources[:5]):
        print(f"  [{i}] llm_score={s.get('llm_score'):.1f} mul={s.get('diversity_mul'):.2f} "
              f"src={s['source'][:55]} p.{s['page']}")
        print(f"      reason: {s.get('llm_reason', '')}")

    if debug:
        us = debug.get("usage", {})
        print(f"\n  USAGE — cache_create={us.get('cache_creation_input_tokens')}, "
              f"cache_read={us.get('cache_read_input_tokens')}, "
              f"input={us.get('input_tokens')}, output={us.get('output_tokens')}")


if __name__ == "__main__":
    tests = [
        ("법령 체계 위계와 헌법의 위치를 설명해줘", "[A] hierarchy"),
    ]
    for q, label in tests:
        test(q, label)
        time.sleep(2)
