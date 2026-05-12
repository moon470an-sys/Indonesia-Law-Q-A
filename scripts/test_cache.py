"""Anthropic prompt caching + thinking 호환성 격리 테스트."""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
from anthropic import Anthropic

client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

# 1024 토큰 이상 보장: 영문 padding 반복으로 확실히
LONG_SYSTEM = (
    "You are a legal expert specializing in Indonesian law. "
    "Provide accurate answers based on the provided context.\n\n"
    + "## Detailed legal analysis guidelines:\n"
    + "\n".join([f"- Guideline {i}: provide thorough analysis with citations to specific articles, pages, and sections of the relevant laws. Always reference Pasal numbers, page numbers, and exact filenames when citing." for i in range(1, 60)])
)
print(f"system length: {len(LONG_SYSTEM)} chars")

def hit(label, *, use_thinking, system_as_list):
    sys = (
        [{"type": "text", "text": LONG_SYSTEM, "cache_control": {"type": "ephemeral"}}]
        if system_as_list else LONG_SYSTEM
    )
    kw = dict(
        model="claude-sonnet-4-6",
        max_tokens=200,
        system=sys,
        messages=[{"role": "user", "content": "What is the hierarchy of Indonesian law?"}],
    )
    if use_thinking:
        kw["max_tokens"] = 3000
        kw["thinking"] = {"type": "enabled", "budget_tokens": 1024}
    resp = client.messages.create(**kw)
    u = resp.usage
    print(f"  [{label}] cache_create={getattr(u, 'cache_creation_input_tokens', 0)}, "
          f"cache_read={getattr(u, 'cache_read_input_tokens', 0)}, "
          f"input={u.input_tokens}, output={u.output_tokens}")

print("\n=== Test 1: list system + cache_control, NO thinking ===")
hit("call 1", use_thinking=False, system_as_list=True)
hit("call 2", use_thinking=False, system_as_list=True)

print("\n=== Test 2: list system + cache_control + thinking ===")
hit("call 1", use_thinking=True, system_as_list=True)
hit("call 2", use_thinking=True, system_as_list=True)
