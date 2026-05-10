# Indonesia Law RAG - long-running watchdog
# uvicorn + cloudflared 살아있게 유지, 터널 URL 자동 publish (frontend/tunnel.json -> GitHub Pages).
# Task Scheduler logon trigger로 한 번 실행. 죽으면 자동 재시작 (Restart on failure).

$ErrorActionPreference = "Continue"

$Project     = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$Python      = Join-Path $Project ".venv\Scripts\python.exe"
$LogDir      = Join-Path $Project "logs"
$Cloudflared = "C:\Users\yoonseok.moon\AppData\Local\Microsoft\WinGet\Packages\Cloudflare.cloudflared_Microsoft.Winget.Source_8wekyb3d8bbwe\cloudflared.exe"
$PageBase    = "https://moon470an-sys.github.io/Indonesia-Law-Q-A/"
$ShortcutName= "Indonesia Law Q&A.url"
$TunnelJson  = Join-Path $Project "frontend\tunnel.json"
$TunnelTxt   = Join-Path $Project ".tunnel_url"

$null = New-Item -ItemType Directory -Force -Path $LogDir

function Write-WLog {
    param([string]$Msg)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "[$ts] $Msg" | Out-File "$LogDir\watchdog.log" -Append -Encoding utf8
}

# Sleep prevention (group policy blocks powercfg, so use API)
Add-Type -ErrorAction SilentlyContinue -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class SleepGuard {
    [DllImport("kernel32.dll", SetLastError=true)]
    public static extern uint SetThreadExecutionState(uint esFlags);
    public const uint ES_CONTINUOUS = 0x80000000;
    public const uint ES_SYSTEM_REQUIRED = 0x00000001;
    public const uint ES_DISPLAY_REQUIRED = 0x00000002;
}
"@
[SleepGuard]::SetThreadExecutionState([SleepGuard]::ES_CONTINUOUS -bor [SleepGuard]::ES_SYSTEM_REQUIRED) | Out-Null
Write-WLog "=== watchdog started (pid=$PID) sleep prevention active ==="

if (-not (Test-Path $Python))      { Write-WLog "FATAL: python.exe missing ($Python)"; exit 1 }
if (-not (Test-Path $Cloudflared)) { Write-WLog "FATAL: cloudflared.exe missing"; exit 1 }
if (-not (Test-Path (Join-Path $Project ".env"))) { Write-WLog "FATAL: .env missing"; exit 1 }

function Test-UvicornHealth {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:8000/health" -UseBasicParsing -TimeoutSec 5
        return ($r.StatusCode -eq 200)
    } catch { return $false }
}

function Test-TunnelHealth {
    param([string]$Url)
    if (-not $Url) { return $false }
    try {
        $r = Invoke-WebRequest -Uri "$Url/health" -UseBasicParsing -TimeoutSec 15
        return ($r.StatusCode -eq 200)
    } catch { return $false }
}

function Stop-AllUvicorn {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -match "rag_server:app" -or ($_.CommandLine -match "uvicorn" -and $_.CommandLine -match "rag_server") } |
        ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            Write-WLog "  killed old uvicorn pid=$($_.ProcessId)"
        }
}

function Stop-AllCloudflared {
    Get-CimInstance Win32_Process -Filter "Name='cloudflared.exe'" |
        ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            Write-WLog "  killed old cloudflared pid=$($_.ProcessId)"
        }
}

function Start-Uvicorn {
    Write-WLog "starting uvicorn..."
    Stop-AllUvicorn
    Start-Sleep -Seconds 2
    $uvLog = Join-Path $LogDir "uvicorn.log"
    $uvErr = Join-Path $LogDir "uvicorn.err"
    $proc = Start-Process -FilePath $Python `
        -ArgumentList "-m","uvicorn","rag_server:app","--host","127.0.0.1","--port","8000" `
        -WorkingDirectory $Project -WindowStyle Hidden `
        -RedirectStandardOutput $uvLog -RedirectStandardError $uvErr -PassThru
    Write-WLog "  uvicorn pid=$($proc.Id), polling /health up to 10 min"
    for ($i = 0; $i -lt 150; $i++) {
        if (Test-UvicornHealth) {
            Write-WLog "  uvicorn healthy after $($i*4)s"
            return $true
        }
        Start-Sleep -Seconds 4
    }
    Write-WLog "  ERR: uvicorn /health did not respond in 10 min"
    return $false
}

function Start-Cloudflared {
    Write-WLog "starting cloudflared..."
    Stop-AllCloudflared
    Start-Sleep -Seconds 2
    $cfOut = Join-Path $LogDir "cloudflared.log"
    $cfErr = Join-Path $LogDir "cloudflared.err"

    for ($attempt = 1; $attempt -le 5; $attempt++) {
        if (Test-Path $cfErr) { Remove-Item $cfErr -Force -ErrorAction SilentlyContinue }
        $proc = Start-Process -FilePath $Cloudflared `
            -ArgumentList "tunnel","--url","http://127.0.0.1:8000" `
            -WindowStyle Hidden `
            -RedirectStandardOutput $cfOut -RedirectStandardError $cfErr -PassThru
        Write-WLog "  attempt $attempt cloudflared pid=$($proc.Id)"
        $url = $null
        $bad = $false
        for ($i = 0; $i -lt 60; $i++) {
            if (Test-Path $cfErr) {
                $m = Select-String -Path $cfErr -Pattern "https://[a-z0-9-]+\.trycloudflare\.com" -ErrorAction SilentlyContinue | Select-Object -First 1
                if ($m) { $url = $m.Matches[0].Value; break }
                $err = Select-String -Path $cfErr -Pattern "Error unmarshaling QuickTunnel response" -ErrorAction SilentlyContinue | Select-Object -First 1
                if ($err) { $bad = $true; break }
            }
            Start-Sleep -Seconds 1
        }
        if ($url) {
            Write-WLog "  got tunnel URL: $url"
            return $url
        }
        Write-WLog "  attempt $attempt failed (bad500=$bad), killing pid=$($proc.Id)"
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 5
    }
    Write-WLog "  ERR: failed to obtain tunnel URL after 5 attempts"
    return $null
}

function Publish-TunnelUrl {
    param([string]$Url)
    if (-not $Url) { return }

    $Url | Out-File $TunnelTxt -Encoding utf8

    $ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    $json = @{ url = $Url; updated = $ts } | ConvertTo-Json -Compress
    $json | Out-File $TunnelJson -Encoding utf8 -NoNewline
    Write-WLog "  wrote tunnel.json"

    $encoded = [Uri]::EscapeDataString($Url)
    $desktop = [Environment]::GetFolderPath("Desktop")
    $shortcutPath = Join-Path $desktop $ShortcutName
    $content = "[InternetShortcut]`r`nURL=$PageBase" + "?api=$encoded`r`n"
    [System.IO.File]::WriteAllText($shortcutPath, $content, [System.Text.Encoding]::ASCII)
    Write-WLog "  shortcut updated: $shortcutPath"

    Push-Location $Project
    try {
        & git add frontend/tunnel.json 2>&1 | Out-Null
        $diff = & git status --porcelain -- frontend/tunnel.json
        if ($diff) {
            & git commit -m "watchdog: update tunnel URL" -- frontend/tunnel.json 2>&1 | Out-Null
            $pushOut = (& git push origin main 2>&1) -join " | "
            Write-WLog "  git push: $pushOut"
        } else {
            Write-WLog "  tunnel.json unchanged, skip git push"
        }
    } catch {
        Write-WLog "  git push failed: $_"
    } finally {
        Pop-Location
    }
}

# === MAIN LOOP ===
$currentTunnel = ""
if (Test-Path $TunnelTxt) { $currentTunnel = (Get-Content $TunnelTxt -Raw).Trim() }
Write-WLog "boot: known tunnel = $currentTunnel"

while ($true) {
    try {
        if (-not (Test-UvicornHealth)) {
            Write-WLog "uvicorn DOWN, restarting"
            if (-not (Start-Uvicorn)) {
                Write-WLog "uvicorn restart failed; sleep 60s and retry"
                Start-Sleep -Seconds 60
                continue
            }
        }

        $cfOk = $false
        if ($currentTunnel) { $cfOk = Test-TunnelHealth -Url $currentTunnel }
        if (-not $cfOk) {
            Write-WLog "tunnel DOWN (current=$currentTunnel), restarting cloudflared"
            $newUrl = Start-Cloudflared
            if ($newUrl) {
                $currentTunnel = $newUrl
                Publish-TunnelUrl -Url $newUrl
                # 발급 직후 propagation 대기
                Start-Sleep -Seconds 10
            } else {
                Write-WLog "cloudflared restart failed; sleep 60s and retry"
            }
        }
    } catch {
        Write-WLog "loop error: $_"
    }
    Start-Sleep -Seconds 60
}
