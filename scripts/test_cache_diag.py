"""Phase 8: cache 진단 — system 단독 token + tools 위치 변경 시도."""
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")
from anthropic import Anthropic
import rag_server_v2 as r

client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

print("=== system 단독 token 측정 ===")
tk = client.messages.count_tokens(
    model="claude-sonnet-4-6",
    system=r.SYSTEM_PROMPT_CACHED,
    messages=[{"role": "user", "content": "x"}],
)
print(f"system+tiny_user: {tk.input_tokens}")

print("\n=== system+tools 합산 token (Phase 8 시도: tools에도 cache_control) ===")
tools_with_cache = [
    *r.TOOLS[:-1],
    {**r.TOOLS[-1], "cache_control": {"type": "ephemeral"}},
]
tk2 = client.messages.count_tokens(
    model="claude-sonnet-4-6",
    system=r.SYSTEM_PROMPT_CACHED,
    tools=tools_with_cache,
    messages=[{"role": "user", "content": "x"}],
)
print(f"system+tools+tiny_user: {tk2.input_tokens}")

print("\n=== 실제 호출 (system cache_control + tools cache_control) ===")
for i in range(2):
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        system=r.SYSTEM_PROMPT_CACHED,
        tools=tools_with_cache,
        messages=[{"role": "user", "content": "인도네시아 법령 1단어"}],
    )
    u = resp.usage
    print(f"  call {i+1}: cache_create={u.cache_creation_input_tokens}, "
          f"cache_read={u.cache_read_input_tokens}, "
          f"input={u.input_tokens}, output={u.output_tokens}")

print("\n=== 실제 호출 (tools 마지막에만 cache_control, system string로) ===")
tools_only = [
    *r.TOOLS[:-1],
    {**r.TOOLS[-1], "cache_control": {"type": "ephemeral"}},
]
for i in range(2):
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=200,
        system=r.SYSTEM_PROMPT,  # 그냥 string
        tools=tools_only,
        messages=[{"role": "user", "content": "인도네시아 법령 1단어"}],
    )
    u = resp.usage
    print(f"  call {i+1}: cache_create={u.cache_creation_input_tokens}, "
          f"cache_read={u.cache_read_input_tokens}, "
          f"input={u.input_tokens}, output={u.output_tokens}")
