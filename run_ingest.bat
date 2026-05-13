@echo off
REM 페이지파일 32GB+ 확장 후 사용하는 가속 설정.
REM workers=5 (모델 ~1GB/워커 × 5 = 5GB), batch=300 (풀 재생성 시 메모리 회수 빠르게)
REM embed_batch=96 (워커당 인코딩 효율↑), flush=512 (ChromaDB upsert 자주 → 버퍼 누적↓)
set RAG_PARSE_WORKERS=5
set RAG_BATCH_SIZE=300
set RAG_EMBED_BATCH=96
set RAG_PARSE_TIMEOUT=120
set RAG_UPSERT_FLUSH_CHUNKS=512
set RAG_MAX_PDF_BYTES=62914560
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
REM venv는 OneDrive 외부에 보관 (자세한 이유는 auto_start\watchdog.ps1 주석 참고).
if not defined RAG_VENV_DIR set RAG_VENV_DIR=D:\venvs\rag_indonesia_law
"%RAG_VENV_DIR%\Scripts\python.exe" -u ingest_loop.py --duration 48h --interval 60s
