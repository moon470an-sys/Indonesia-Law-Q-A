# Indonesia Law RAG - end-to-end stack verification.
# Goes through the real user path: Pages -> tunnel.json -> tunnel /health -> data.
# Probes a second time after a delay to make sure it's stable, not a one-off flake.

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

# 1. Watchdog + uvicorn + cloudflared processes
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

Step "local /health quick (cache HIT expected after prewarm)" {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:8000/health?quick=1" -UseBasicParsing -TimeoutSec 10
    $d = $r.Content | ConvertFrom-Json
    if (-not $d.ok) { throw "ok=false" }
    if ($d.warming) { throw "still warming - prewarm did not populate cache yet" }
    if (-not $d.collection_count -or $d.collection_count -lt 1) { throw "collection_count=$($d.collection_count)" }
    "(count=$($d.collection_count))"
}

# 3. Pages tunnel.json
$script:_pagesTunnel = $null
Step "Pages tunnel.json fetch (timeout 15s)" {
    $r = Invoke-WebRequest -Uri "$PagesBase/tunnel.json?t=$(Get-Date -Format yyyyMMddHHmmss)" -UseBasicParsing -TimeoutSec 15
    if ($r.StatusCode -ne 200) { throw "status=$($r.StatusCode)" }
    $text = $r.Content
    if ($text.Length -gt 0 -and $text[0] -eq [char]0xFEFF) { $text = $text.Substring(1) }
    $d = $text | ConvertFrom-Json
    if (-not $d.url) { throw "no url field" }
    $script:_pagesTunnel = $d.url
    "(url=$($d.url) updated=$($d.updated))"
}

# 4. Fetch tunnel URL from outside
if ($script:_pagesTunnel) {
    Step "tunnel /healthz from outside (timeout 15s)" {
        $r = Invoke-WebRequest -Uri "$($script:_pagesTunnel)/healthz" -UseBasicParsing -TimeoutSec 15
        if ($r.StatusCode -ne 200) { throw "status=$($r.StatusCode)" }
        "(200)"
    }

    Step "tunnel /health quick from outside (cache HIT, count>0)" {
        $r = Invoke-WebRequest -Uri "$($script:_pagesTunnel)/health?quick=1" -UseBasicParsing -TimeoutSec 30
        $d = $r.Content | ConvertFrom-Json
        if (-not $d.ok) { throw "ok=false" }
        if ($d.warming) { throw "still warming through tunnel" }
        if (-not $d.collection_count -or $d.collection_count -lt 1) { throw "collection_count=$($d.collection_count)" }
        "(count=$($d.collection_count))"
    }
}

# 5. Stability re-check after a 15s sleep
Write-Host ""
Write-Host "[ .. ] sleeping 15s for stability re-check..." -ForegroundColor DarkGray
Start-Sleep -Seconds 15

Step "local /healthz re-check after 15s" {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:8000/healthz" -UseBasicParsing -TimeoutSec 5
    if ($r.StatusCode -ne 200) { throw "status=$($r.StatusCode)" }
    "(200)"
}

if ($script:_pagesTunnel) {
    Step "tunnel /healthz re-check after 15s" {
        $r = Invoke-WebRequest -Uri "$($script:_pagesTunnel)/healthz" -UseBasicParsing -TimeoutSec 15
        if ($r.StatusCode -ne 200) { throw "status=$($r.StatusCode)" }
        "(200)"
    }
}

# 6. Verify deployed app.js carries the new fetchWithTimeout marker
Step "Pages app.js deployed with fetchWithTimeout" {
    $r = Invoke-WebRequest -Uri "$PagesBase/app.js?t=$(Get-Date -Format yyyyMMddHHmmss)" -UseBasicParsing -TimeoutSec 15
    if ($r.Content -notmatch "fetchWithTimeout") { throw "marker missing - Pages may be serving stale build" }
    "(deployed)"
}

Write-Host ""
Write-Host "=== Summary ===" -ForegroundColor Cyan
Write-Host "PASS: $($Pass.Count)" -ForegroundColor Green
$failColor = if ($Fail.Count -gt 0) { "Red" } else { "Green" }
Write-Host "FAIL: $($Fail.Count)" -ForegroundColor $failColor
if ($Fail.Count -gt 0) {
    Write-Host ""
    Write-Host "Failures:" -ForegroundColor Red
    $Fail | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
    exit 1
}
Write-Host ""
Write-Host "All checks passed." -ForegroundColor Green
exit 0
