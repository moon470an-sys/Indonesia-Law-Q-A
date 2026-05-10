# Indonesia Law RAG - logon auto-start script
# Called by Task Scheduler. Safe to run manually too.
# Encoding-agnostic: uses $PSScriptRoot, avoids non-ASCII string literals.

$ErrorActionPreference = "Continue"

$Project   = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python    = Join-Path $Project ".venv\Scripts\python.exe"
$LogDir    = Join-Path $Project "logs"
$Cloudflared = "C:\Users\yoonseok.moon\AppData\Local\Microsoft\WinGet\Packages\Cloudflare.cloudflared_Microsoft.Winget.Source_8wekyb3d8bbwe\cloudflared.exe"
$PageBase  = "https://moon470an-sys.github.io/Indonesia-Law-Q-A/"
$ShortcutName = "Indonesia Law Q&A.url"

$null = New-Item -ItemType Directory -Force -Path $LogDir

function Write-Log {
    param([string]$Msg)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "[$ts] $Msg" | Out-File "$LogDir\start.log" -Append -Encoding utf8
}

Write-Log "=== auto-start invoked ==="
Write-Log "Project: $Project"

# 0) preconditions
if (-not (Test-Path $Python))      { Write-Log "ERR: python.exe missing ($Python)"; exit 1 }
if (-not (Test-Path $Cloudflared)) { Write-Log "ERR: cloudflared.exe missing ($Cloudflared)"; exit 1 }
if (-not (Test-Path (Join-Path $Project ".env"))) { Write-Log "ERR: .env missing"; exit 1 }

# 1) clean leftover processes
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match "rag_server:app" } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Log "killed old uvicorn pid=$($_.ProcessId)"
    }

Get-CimInstance Win32_Process -Filter "Name='cloudflared.exe'" |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        Write-Log "killed old cloudflared pid=$($_.ProcessId)"
    }

Start-Sleep -Seconds 2

# 2) start uvicorn
$uvLog = "$LogDir\uvicorn.log"
$uvErr = "$LogDir\uvicorn.err"
$uvProc = Start-Process -FilePath $Python `
    -ArgumentList "-m", "uvicorn", "rag_server:app", "--host", "127.0.0.1", "--port", "8000" `
    -WorkingDirectory $Project `
    -WindowStyle Hidden `
    -RedirectStandardOutput $uvLog `
    -RedirectStandardError $uvErr `
    -PassThru
Write-Log "uvicorn started pid=$($uvProc.Id)"

# 3) wait for /health (max ~10 min — 2.5M청크 8컬렉션 ChromaDB 로드가 3분+ 걸림)
$healthy = $false
for ($i = 0; $i -lt 150; $i++) {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:8000/health" -UseBasicParsing -TimeoutSec 4
        if ($r.StatusCode -eq 200) { $healthy = $true; break }
    } catch {}
    Start-Sleep -Seconds 4
}
if (-not $healthy) { Write-Log "ERR: uvicorn /health did not respond within 10 min"; exit 1 }
Write-Log "uvicorn healthy after $($i*4)s"

# 4) start cloudflared
$cfOut = "$LogDir\cloudflared.log"
$cfErr = "$LogDir\cloudflared.err"
if (Test-Path $cfErr) { Remove-Item $cfErr -Force }
$cfProc = Start-Process -FilePath $Cloudflared `
    -ArgumentList "tunnel", "--url", "http://127.0.0.1:8000" `
    -WindowStyle Hidden `
    -RedirectStandardOutput $cfOut `
    -RedirectStandardError $cfErr `
    -PassThru
Write-Log "cloudflared started pid=$($cfProc.Id)"

# 5) extract URL (cloudflared logs to stderr)
$tunnelUrl = $null
for ($i = 0; $i -lt 60; $i++) {
    if (Test-Path $cfErr) {
        $m = Select-String -Path $cfErr -Pattern "https://[a-z0-9-]+\.trycloudflare\.com" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($m) { $tunnelUrl = $m.Matches[0].Value; break }
    }
    Start-Sleep -Seconds 1
}
if (-not $tunnelUrl) { Write-Log "ERR: tunnel URL not found within 60 sec"; exit 1 }
Write-Log "tunnel URL: $tunnelUrl"

# 6) save URL + update desktop shortcut
$tunnelUrl | Out-File (Join-Path $Project ".tunnel_url") -Encoding utf8

$encoded = [Uri]::EscapeDataString($tunnelUrl)
$desktop = [Environment]::GetFolderPath("Desktop")
$shortcutPath = Join-Path $desktop $ShortcutName
$content = "[InternetShortcut]`r`nURL=$PageBase" + "?api=$encoded`r`n"
[System.IO.File]::WriteAllText($shortcutPath, $content, [System.Text.Encoding]::ASCII)

Write-Log "shortcut updated: $shortcutPath"

# 7) ingest 자동 재개 — 비활성화 (2026-05-10).
#    인덱싱은 1회 마무리 완료. 남은 1,629건은 텍스트 추출 불가 PDF(스캔/이미지)라
#    매 사이클 0 청크 반환만 반복하므로 자동 재개 시 CPU만 낭비. OCR 도입 전까지 보류.
#    수동으로 다시 돌리려면: powershell -ExecutionPolicy Bypass -File auto_start\resume_ingest.ps1

Write-Log "=== done ==="
exit 0
