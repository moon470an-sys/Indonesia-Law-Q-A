"""Claude Desktop (MSIX/UWP) config에 indonesia-law-rag MCP server 등록.

UWP sandbox의 가상 %APPDATA%\\Claude\\claude_desktop_config.json은
실제로는 C:\\Users\\<user>\\AppData\\Local\\Packages\\Claude_pzs8sxrjxfjjc\\
LocalCache\\Roaming\\Claude\\claude_desktop_config.json 에 있다.
기존 preferences 키는 보존하고 mcpServers만 추가/덮어쓴다.
"""
import json
import os
import shutil
from pathlib import Path

CFG = (Path(os.environ["LOCALAPPDATA"])
       / "Packages" / "Claude_pzs8sxrjxfjjc"
       / "LocalCache" / "Roaming" / "Claude"
       / "claude_desktop_config.json")

PROJECT = Path(__file__).resolve().parent.parent
# venv는 OneDrive 외부(D:)에 보관 — OneDrive Files On-Demand가 .venv 수천 개
# 파일을 reify하면서 file handle / mmap 자원이 고갈되는 사고 회피.
VENV = Path(os.environ.get("RAG_VENV_DIR", r"D:\venvs\rag_indonesia_law"))
PYTHON = str(VENV / "Scripts" / "python.exe")
SCRIPT = str(PROJECT / "mcp_server.py")

mcp_entry = {
    "command": PYTHON,
    "args": [SCRIPT],
    "env": {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
}

if not CFG.exists():
    raise SystemExit(f"config not found: {CFG}\nClaude Desktop을 한 번 실행한 적이 있는지 확인하세요.")

# backup
bak = CFG.with_suffix(CFG.suffix + ".bak")
shutil.copy(CFG, bak)

with CFG.open("r", encoding="utf-8") as f:
    data = json.load(f)

if not isinstance(data, dict):
    raise SystemExit(f"unexpected JSON root type: {type(data).__name__}")

servers = data.get("mcpServers") or {}
servers["indonesia-law-rag"] = mcp_entry
data["mcpServers"] = servers

with CFG.open("w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)

print(f"OK — wrote {CFG}")
print(f"backup: {bak}")
print(f"command: {PYTHON}")
print(f"args[0]: {SCRIPT}")
print("\n현재 mcpServers 키:", list(servers.keys()))
