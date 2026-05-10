# Indonesia Law RAG - thin wrapper.
# Real work happens in watchdog.ps1 (long-running supervisor that restarts uvicorn/cloudflared
# and republishes tunnel URL to frontend/tunnel.json on GitHub Pages).
& "$PSScriptRoot\watchdog.ps1"
