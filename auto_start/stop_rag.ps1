# Indonesia Law RAG - manual stop
$ErrorActionPreference = "Continue"

Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match "rag_server:app" } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Output "uvicorn stopped (pid=$($_.ProcessId))"
    }

Get-CimInstance Win32_Process -Filter "Name='cloudflared.exe'" |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Output "cloudflared stopped (pid=$($_.ProcessId))"
    }

Write-Output "done"
