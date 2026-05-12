"""Phase 7: conversation memory — turn 1 → turn 2 follow-up 검증."""
import json
import time
import urllib.request


def call(question: str, conversation_id: str | None, label: str) -> dict:
    print(f"\n{'='*70}\n{label}\nQ: {question}\nconv_id sent: {conversation_id or '(none)'}\n{'='*70}")
    payload = {"question": question}
    if conversation_id:
        payload["conversation_id"] = conversation_id
    payload_b = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:8000/query/stream",
        data=payload_b,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    conv = None
    answer = []
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
                if event == "conversation":
                    conv = data
                    print(f"  [{now:.2f}s] conversation: id={data.get('conversation_id')} turn={data.get('turn')} followup={data.get('is_followup')}")
                elif event == "intent":
                    print(f"  [{now:.2f}s] intent={data.get('intent')}")
                elif event == "token":
                    answer.append(data.get("text", ""))
                elif event == "done":
                    debug = data.get("debug")

    total = time.time() - t0
    full = "".join(answer)
    print(f"\n  TIMING — total={total:.2f}s, answer={len(full)}자")
    print(f"  ANSWER preview: {full[:300]!r}")
    if debug and debug.get("conversation"):
        cv = debug["conversation"]
        print(f"  CONV debug: id={cv.get('id')}, turn={cv.get('turn')}, loaded_msgs={cv.get('history_messages_loaded')}")
    return {"conv_id": conv.get("conversation_id") if conv else None, "answer": full, "debug": debug}


if __name__ == "__main__":
    # Turn 1 — 새 대화
    r1 = call("인도네시아 법령 위계를 표로 정리해줘", None, "[Turn 1] 새 대화 시작")
    cid = r1["conv_id"]
    time.sleep(2)

    # Turn 2 — 같은 conv_id로 follow-up
    r2 = call("방금 답변에서 Pasal 7의 인용을 더 자세히 인도네시아어 원문으로 보여줘", cid, "[Turn 2] 이전 답변 참조 follow-up")
    time.sleep(2)

    # Turn 3 — 또 follow-up
    r3 = call("이걸 표 대신 다이어그램처럼 정리해줘", cid, "[Turn 3] 같은 대화 이어가기")
