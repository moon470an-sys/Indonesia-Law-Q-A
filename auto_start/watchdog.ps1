# Indonesia Law RAG - long-running watchdog
# uvicorn + cloudflared 살아있게 유지, 터널 URL 자동 publish (frontend/tunnel.json -> GitHub Pages).
# Task Scheduler logon trigger로 한 번 실행. 죽으면 자동 재시작 (Restart on failure).

$ErrorActionPreference = "Continue"

$Project     = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
# venv는 OneDrive 외부(D:)에 둔다. OneDrive Files On-Demand가 .venv 안 수천 개
# 파일을 reify하면서 file handle / non-paged pool이 폭주, BGE-M3·ChromaDB의
# mmap이 깨지고 WinError 1450 / chroma TCP 바인딩 5분 타임아웃이 발생.
# 환경변수 RAG_VENV_DIR가 있으면 그걸 우선.
$VenvDir     = if ($env:RAG_VENV_DIR) { $env:RAG_VENV_DIR } else { "D:\venvs\rag_indonesia_law" }
$Python      = Join-Path $VenvDir "Scripts\python.exe"
$ChromaExe   = Join-Path $VenvDir "Scripts\chroma.exe"
$ChromaPath  = "D:\rag_data\chroma_db"
$ChromaHost  = "127.0.0.1"
$ChromaPort  = 8001
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
if (-not (Test-Path $ChromaExe))   { Write-WLog "FATAL: chroma.exe missing ($ChromaExe)"; exit 1 }
if (-not (Test-Path $Cloudflared)) { Write-WLog "FATAL: cloudflared.exe missing"; exit 1 }
if (-not (Test-Path (Join-Path $Project ".env"))) { Write-WLog "FATAL: .env missing"; exit 1 }

function Test-UvicornHealth {
    # /healthz는 ChromaDB 접근 없이 즉시 응답하는 liveness 엔드포인트.
    # 30s 타임아웃으로 일시적 GC/IO 지연도 흡수.
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:8000/healthz" -UseBasicParsing -TimeoutSec 30
        return ($r.StatusCode -eq 200)
    } catch { return $false }
}

function Test-TunnelHealth {
    param([string]$Url)
    if (-not $Url) { return $false }
    try {
        $r = Invoke-WebRequest -Uri "$Url/healthz" -UseBasicParsing -TimeoutSec 30
        return ($r.StatusCode -eq 200)
    } catch { return $false }
}

$UvicornPidFile     = Join-Path $LogDir "uvicorn.pid"
$CloudflaredPidFile = Join-Path $LogDir "cloudflared.pid"
$ChromaPidFile      = Join-Path $LogDir "chroma.pid"

function Stop-PidFromFile {
    # WMI/CIM (Get-CimInstance Win32_Process)는 좀비 프로세스가 쌓이면 hang하는 사례 있음.
    # PID 파일로 watchdog가 직접 띄운 프로세스만 식별해서 종료한다.
    param([string]$PidFile, [string]$Label)
    if (-not (Test-Path $PidFile)) { return }
    $oldPid = (Get-Content $PidFile -Raw -ErrorAction SilentlyContinue).Trim()
    if ($oldPid) {
        $p = Get-Process -Id $oldPid -ErrorAction SilentlyContinue
        if ($p) {
            Stop-Process -Id $oldPid -Force -ErrorAction SilentlyContinue
            Write-WLog "  killed old $Label pid=$oldPid"
        }
    }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
}

function Stop-AllUvicorn {
    Stop-PidFromFile -PidFile $UvicornPidFile -Label "uvicorn"
}

function Test-ChromaHealth {
    # TCP connect on $ChromaPort — chroma server LISTEN이면 OK. API 버전(v1/v2) 의존 안 함.
    try {
        $c = New-Object System.Net.Sockets.TcpClient
        $async = $c.BeginConnect($ChromaHost, $ChromaPort, $null, $null)
        $ok = $async.AsyncWaitHandle.WaitOne(2000, $false)
        if ($ok -and $c.Connected) { $c.EndConnect($async); $c.Close(); return $true }
        $c.Close()
        return $false
    } catch { return $false }
}

function Stop-AllChroma {
    Stop-PidFromFile -PidFile $ChromaPidFile -Label "chroma"
    # 추가 안전망: stray chroma.exe도 정리. Get-Process는 WMI 안 씀.
    Get-Process chroma -ErrorAction SilentlyContinue | ForEach-Object {
        Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
        Write-WLog "  killed stray chroma pid=$($_.Id)"
    }
}

function Start-Chroma {
    Write-WLog "starting chroma..."
    Stop-AllChroma
    Start-Sleep -Seconds 2
    $chOut = Join-Path $LogDir "chroma_server.log"
    $chErr = Join-Path $LogDir "chroma_server.err"
    $proc = Start-Process -FilePath $ChromaExe `
        -ArgumentList "run","--path",$ChromaPath,"--host",$ChromaHost,"--port","$ChromaPort" `
        -WorkingDirectory $Project -WindowStyle Hidden `
        -RedirectStandardOutput $chOut -RedirectStandardError $chErr -PassThru
    "$($proc.Id)" | Out-File -FilePath $ChromaPidFile -Encoding ascii -NoNewline
    Write-WLog "  chroma pid=$($proc.Id), polling TCP $ChromaHost`:$ChromaPort up to 5 min"
    # cold start: 인덱스 mmap 시간 필요. 250만 청크면 1~3분 예상.
    for ($i = 0; $i -lt 75; $i++) {
        if (Test-ChromaHealth) {
            Write-WLog "  chroma healthy after $($i*4)s"
            return $true
        }
        Start-Sleep -Seconds 4
    }
    Write-WLog "  ERR: chroma did not bind $ChromaPort in 5 min"
    return $false
}

function Stop-AllCloudflared {
    Stop-PidFromFile -PidFile $CloudflaredPidFile -Label "cloudflared"
    # 추가 안전망: 다른 경로로 떠 있는 stray cloudflared까지 정리. Get-Process는 WMI 안 씀.
    Get-Process cloudflared -ErrorAction SilentlyContinue | ForEach-Object {
        Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
        Write-WLog "  killed stray cloudflared pid=$($_.Id)"
    }
}

function Invoke-HealthPrewarm {
    # 새 uvicorn 기동 직후 /health(slow path)를 한 번 호출해 캐시를 채워둠.
    # FastAPI의 sync def → anyio threadpool로 위임돼서 이벤트루프 안 막고 캐시만 채움.
    # 사용자 첫 요청은 cache hit으로 즉답.
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:8000/health" `
            -UseBasicParsing -TimeoutSec 300
        Write-WLog "  /health prewarm OK ($($r.StatusCode), bytes=$($r.Content.Length))"
    } catch {
        Write-WLog "  /health prewarm failed: $_"
    }
}

function Start-Uvicorn {
    Write-WLog "starting uvicorn..."
    Stop-AllUvicorn
    Start-Sleep -Seconds 2
    $uvLog = Join-Path $LogDir "uvicorn.log"
    $uvErr = Join-Path $LogDir "uvicorn.err"
    $proc = Start-Process -FilePath $Python `
        -ArgumentList "-m","uvicorn","rag_server_v2:app","--host","127.0.0.1","--port","8000" `
        -WorkingDirectory $Project -WindowStyle Hidden `
        -RedirectStandardOutput $uvLog -RedirectStandardError $uvErr -PassThru
    "$($proc.Id)" | Out-File -FilePath $UvicornPidFile -Encoding ascii -NoNewline
    Write-WLog "  uvicorn pid=$($proc.Id), polling /health up to 10 min"
    for ($i = 0; $i -lt 150; $i++) {
        if (Test-UvicornHealth) {
            Write-WLog "  uvicorn healthy after $($i*4)s"
            Invoke-HealthPrewarm
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
        "$($proc.Id)" | Out-File -FilePath $CloudflaredPidFile -Encoding ascii -NoNewline
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

# 부팅 시점 chroma 즉시 기동. RAG_CHROMA_MODE=http에서 rag_server가 8001로 붙어야 동작.
if (-not (Test-ChromaHealth)) {
    if (-not (Start-Chroma)) {
        Write-WLog "boot: chroma start failed, will retry in main loop"
    }
}

$lastHealthRefresh = [DateTime]::MinValue

while ($true) {
    try {
        # 0) ChromaDB 데몬 — uvicorn보다 먼저 살아있어야 함. 죽으면 rag_server가 /query에서 실패.
        # TCP 체크 1회 실패 시 즉시 재시작 (uvicorn처럼 일시적 busy로 응답 못 하는 케이스 거의 없음).
        if (-not (Test-ChromaHealth)) {
            Write-WLog "chroma DOWN, restarting"
            if (-not (Start-Chroma)) {
                Write-WLog "chroma restart failed; sleep 60s and retry"
                Start-Sleep -Seconds 60
                continue
            }
        }

        $uvOk = Test-UvicornHealth
        if (-not $uvOk) {
            # /query처럼 무거운 요청 처리 중이면 /healthz가 일시적으로 응답 못함.
            # 5회 연속 실패(약 2.5분 grace)해야 정말 죽었다고 판단. 무차별 재시작 cycle 방지.
            for ($p = 1; $p -le 4; $p++) {
                Start-Sleep -Seconds 30
                if (Test-UvicornHealth) { $uvOk = $true; break }
            }
        }
        if (-not $uvOk) {
            Write-WLog "uvicorn DOWN (5 consecutive checks over ~2.5min failed), restarting"
            $lastHealthRefresh = [DateTime]::MinValue  # 새 uvicorn이면 prewarm이 새로 채움
            if (-not (Start-Uvicorn)) {
                Write-WLog "uvicorn restart failed; sleep 60s and retry"
                Start-Sleep -Seconds 60
                continue
            }
            $lastHealthRefresh = Get-Date  # Start-Uvicorn 내부에서 prewarm 호출됨
        }

        # /health keep-alive 제거: cold start 중에 추가 부하 → /query 더 느려짐 → /healthz timeout → 재시작 cycle.
        # rag_server의 HEALTH_CACHE_TTL=24h라 한번 채워지면 굳이 keep-alive 필요 없음.
        if ($false -and ((Get-Date) - $lastHealthRefresh).TotalMinutes -ge 4) {
            try {
                Invoke-WebRequest -Uri "http://127.0.0.1:8000/health" -UseBasicParsing -TimeoutSec 60 | Out-Null
                $lastHealthRefresh = Get-Date
            } catch {
                Write-WLog "  /health keep-alive failed: $_"
            }
        }

        # 터널 health: 5회 연속 실패해야 URL 재발급. 1~2회 깜빡임은 무시.
        # 매 재발급마다 git push + Pages 빌드 + CDN propagation 비용이 크고, 그 사이 사용자는 stale URL에 묶임.
        # $currentTunnel이 비어있으면 (부팅 첫 사이클) 즉시 시작.
        $cfOk = $false
        if ($currentTunnel) {
            for ($probe = 1; $probe -le 5; $probe++) {
                if (Test-TunnelHealth -Url $currentTunnel) { $cfOk = $true; break }
                if ($probe -lt 5) { Start-Sleep -Seconds 12 }
            }
        }
        if (-not $cfOk) {
            if ($currentTunnel) {
                Write-WLog "tunnel DOWN (current=$currentTunnel, 5 consecutive probes failed), restarting cloudflared"
            } else {
                Write-WLog "tunnel not set, starting cloudflared"
            }
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
