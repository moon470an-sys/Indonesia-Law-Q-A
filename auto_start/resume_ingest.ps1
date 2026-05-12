# Indonesia Law RAG - resume ingest (manifest-based incremental)
# Run any time to continue indexing from where we left off.
# Already-indexed files are skipped automatically via manifest.json.
$ErrorActionPreference = "Continue"

$Project = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$LogDir  = Join-Path $Project "logs"
$Python  = Join-Path $Project ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) { Write-Error "python.exe missing: $Python"; exit 1 }
$null = New-Item -ItemType Directory -Force -Path $LogDir

# Stop any prior ingest first
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match "ingest_loop" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUNBUFFERED = "1"
# PyMuPDF 1차 파서 + pdfplumber fallback 스킵 + upsert 5000 분할 (2026-05-08 최적화).
# 5워커 = 메모리 보수치 (RAM 16GB 시스템). 6워커는 가용 RAM 5GB+일 때만.
$env:RAG_PARSE_WORKERS = "5"
$env:RAG_BATCH_SIZE = "300"
$env:RAG_EMBED_BATCH = "128"
$env:RAG_PARSE_TIMEOUT = "120"
$env:RAG_UPSERT_FLUSH_CHUNKS = "512"
$env:RAG_UPSERT_BATCH_LIMIT = "5000"
$env:RAG_MAX_PDF_BYTES = "62914560"
$env:RAG_SKIP_PDFPLUMBER_FALLBACK = "1"

$outLog = Join-Path $LogDir "ingest_resume.log"
$errLog = Join-Path $LogDir "ingest_resume.err"

$proc = Start-Process -FilePath $Python `
    -ArgumentList "-u", "-X", "utf8", "ingest_loop.py", "--duration", "12h", "--interval", "60s" `
    -WorkingDirectory $Project -WindowStyle Hidden `
    -RedirectStandardOutput $outLog -RedirectStandardError $errLog `
    -PassThru
Write-Output "[OK] Ingest resumed (pid=$($proc.Id))"
Write-Output "Log: $outLog"
Write-Output "Already-indexed files are skipped via manifest. New/changed files only."
