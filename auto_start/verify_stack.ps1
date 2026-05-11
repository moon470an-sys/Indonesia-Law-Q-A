# Indonesia Law RAG - end-to-end stack verification
# 단순 /healthz 200 OK가 아닌 실제 사용자 경로(Pages -> tunnel.json -> /health -> data) 검증.
# 시간차 두 번 호출해서 흔들리는지도 확인.

$ErrorActionPreference = "Continue"

$PagesBase  = "https://moon470an-sys.github.io/Indonesia-Law-Q-A"
$Pass = @()
$Fail = @()

function Step($label, [scriptblock]$body) {
    Write-Host -NoNewline "[ .. ] $label ... "
    try {
        $r = & $body
        Write-Host "OK $r" -ForegroundColor Green
        $script:Pass += $label
        return $r
    } catch {
        Write-Host "FAIL $_" -ForegroundColor Red
        $script:Fail += "$label :: $_"
        return $null
    }
}

Write-Host "=== Indonesia Law RAG stack verification ===" -ForegroundColor Cyan
Write-Host ""

# 1. Watchdog + uvicorn + cloudflared 프로세스 살아있나
Step "watchdog scheduled task RUNNING" {
    $t = Get-ScheduledTask -TaskName "Indonesia Law RAG" -ErrorAction Stop
    $info = $t | Get-ScheduledTaskInfo
    if ($t.State -ne "Running") { throw "state=$($t.State)" }
    "(LastResult=$($info.LastTaskResult))"
}

Step "uvicorn listening on 127.0.0.1:8000" {
    $c = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction Stop
    "(pid=$($c.OwningProcess))"
}

Step "cloudflared process alive" {
    $p = Get-Process -Name cloudflared -ErrorAction Stop
    "(pid=$($p.Id), mem=$([Math]::Round($p.WorkingSet64/1MB,1))MB)"
}

# 2. local /healthz + /health
Step "local /healthz (timeout 5s)" {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:8000/healthz" -UseBasicParsing -TimeoutSec 5
    if ($r.StatusCode -ne 200) { throw "status=$($r.StatusCode)" }
    "(200)"
}

$localHealth = Step "local /health quick (timeout 10s, expect ok=true)" {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:8000/health?quick=1" -UseBasicParsing -TimeoutSec 10
    $d = $r.Content | ConvertFrom-Json
    if (-not $d.ok) { throw "ok=false" }
    "(warming=$($d.warming), count=$($d.collection_count))"
}

# 3. GitHub Pages가 서빙하는 tunnel.json 가져오기
$pagesUrl = Step "Pages tunnel.json fetch (timeout 15s)" {
    $r = Invoke-WebRequest -Uri "$PagesBase/tunnel.json?t=$(Get-Date -Format yyyyMMddHHmmss)" -UseBasicParsing -TimeoutSec 15
    if ($r.StatusCode -ne 200) { throw "status=$($r.StatusCode)" }
    $text = $r.Content -replace "^\xEF\xBB\xBF", "" -replace "^﻿", ""
    $d = $text | ConvertFrom-Json
    if (-not $d.url) { throw "no url in tunnel.json" }
    "(url=$($d.url), updated=$($d.updated))"
    # 부수적으로 URL을 외부 변수로 노출
    $script:_pagesTunnel = $d.url
}

# 4. Pages가 가리키는 터널 URL로 외부에서 healthz 호출
if ($_pagesTunnel) {
    Step "tunnel /healthz from outside (timeout 15s)" {
        $r = Invoke-WebRequest -Uri "$_pagesTunnel/healthz" -UseBasicParsing -TimeoutSec 15
        if ($r.StatusCode -ne 200) { throw "status=$($r.StatusCode)" }
        "(200)"
    }

    Step "tunnel /health quick from outside (timeout 30s)" {
        $r = Invoke-WebRequest -Uri "$_pagesTunnel/health?quick=1" -UseBasicParsing -TimeoutSec 30
        $d = $r.Content | ConvertFrom-Json
        if (-not $d.ok) { throw "ok=false" }
        "(warming=$($d.warming), count=$($d.collection_count))"
    }
}

# 5. 안정성: 15초 대기 후 한번 더 / healthz 호출. 둘 다 성공해야 통과.
Write-Host ""
Write-Host "[ .. ] sleeping 15s for stability re-check..." -ForegroundColor DarkGray
Start-Sleep -Seconds 15
Step "local /healthz re-check after 15s" {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:8000/healthz" -UseBasicParsing -TimeoutSec 5
    if ($r.StatusCode -ne 200) { throw "status=$($r.StatusCode)" }
    "(200)"
}

if ($_pagesTunnel) {
    Step "tunnel /healthz re-check after 15s" {
        $r = Invoke-WebRequest -Uri "$_pagesTunnel/healthz" -UseBasicParsing -TimeoutSec 15
        if ($r.StatusCode -ne 200) { throw "status=$($r.StatusCode)" }
        "(200)"
    }

    # /health 캐시 채워졌는지 (prewarm 효과 확인)
    Step "tunnel /health (non-quick) — cache hit expected" {
        $r = Invoke-WebRequest -Uri "$_pagesTunnel/health" -UseBasicParsing -TimeoutSec 30
        $d = $r.Content | ConvertFrom-Json
        if (-not $d.ok) { throw "ok=false" }
        if (-not $d.collection_count -or $d.collection_count -lt 1) { throw "collection_count=$($d.collection_count)" }
        "(count=$($d.collection_count))"
    }
}

# 6. 정적 자원 확인 — 새 app.js 배포됐는지 (fetchWithTimeout 함수 존재 여부)
Step "Pages app.js contains new fetchWithTimeout" {
    $r = Invoke-WebRequest -Uri "$PagesBase/app.js?t=$(Get-Date -Format yyyyMMddHHmmss)" -UseBasicParsing -TimeoutSec 15
    if ($r.Content -notmatch "fetchWithTimeout") { throw "fetchWithTimeout marker missing in deployed app.js (Pages cache?)" }
    "(deployed)"
}

Write-Host ""
Write-Host "=== Summary ===" -ForegroundColor Cyan
Write-Host "PASS: $($Pass.Count)" -ForegroundColor Green
Write-Host "FAIL: $($Fail.Count)" -ForegroundColor $(if ($Fail.Count -gt 0) { "Red" } else { "Green" })
if ($Fail.Count -gt 0) {
    Write-Host ""
    Write-Host "Failures:" -ForegroundColor Red
    $Fail | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
    exit 1
}
Write-Host ""
Write-Host "All checks passed." -ForegroundColor Green
exit 0
