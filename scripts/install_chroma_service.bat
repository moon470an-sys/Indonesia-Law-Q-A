@echo off
REM ============================================================
REM ChromaDB를 Windows 서비스로 설치 (NSSM 기반).
REM
REM 사전 준비:
REM   1) NSSM 설치:   winget install --id NSSM.NSSM
REM      (또는 https://nssm.cc 에서 다운로드)
REM   2) chromadb이 venv에 설치되어 있어야 함 (기본: D:\venvs\rag_indonesia_law):
REM      "%RAG_VENV_DIR%\Scripts\pip" install -U chromadb
REM      → "%RAG_VENV_DIR%\Scripts\chroma.exe" 가 존재해야 함
REM
REM 실행: 관리자 권한 cmd 또는 PowerShell 에서
REM   scripts\install_chroma_service.bat
REM ============================================================

setlocal enabledelayedexpansion

set PROJECT_DIR=%~dp0..
pushd "%PROJECT_DIR%"
set PROJECT_DIR=%CD%
popd

REM venv는 OneDrive 외부에 둔다 (Files On-Demand가 .venv 수천 개 파일을 reify하면서
REM file handle / non-paged pool이 폭주, mmap이 깨지고 chroma TCP 바인딩 실패).
if not defined RAG_VENV_DIR set RAG_VENV_DIR=D:\venvs\rag_indonesia_law
set VENV=%RAG_VENV_DIR%
set CHROMA_EXE=%VENV%\Scripts\chroma.exe
set CHROMA_PATH=D:\rag_data\chroma_db
set HOST=127.0.0.1
set PORT=8001
set SERVICE_NAME=ChromaDB-IndonesiaLaw
set LOG_DIR=%PROJECT_DIR%\logs

REM 환경변수 RAG_CHROMA_PATH / RAG_CHROMA_PORT 가 미리 설정돼 있으면 그걸 사용
if not "%RAG_CHROMA_PATH%"=="" set CHROMA_PATH=%RAG_CHROMA_PATH%
if not "%RAG_CHROMA_PORT%"=="" set PORT=%RAG_CHROMA_PORT%

if not exist "%CHROMA_EXE%" (
    echo [error] chroma.exe 를 찾을 수 없음: %CHROMA_EXE%
    echo         %VENV%\Scripts\pip install -U chromadb 먼저 실행하세요.
    exit /b 1
)
if not exist "%CHROMA_PATH%" (
    echo [warn] ChromaDB 데이터 경로 없음: %CHROMA_PATH%
    echo        ingest 로 인덱싱하거나 경로를 확인하세요. 서비스는 일단 등록만 진행.
)
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

where nssm >nul 2>nul
if errorlevel 1 (
    echo [error] nssm 명령을 찾을 수 없음. winget install --id NSSM.NSSM 먼저 실행하세요.
    exit /b 1
)

echo === Installing service: %SERVICE_NAME% ===
echo   path = %CHROMA_PATH%
echo   bind = %HOST%:%PORT%
echo   exe  = %CHROMA_EXE%

REM 이미 존재하면 갱신 (제거 후 재등록)
nssm status %SERVICE_NAME% >nul 2>nul
if not errorlevel 1 (
    echo [info] 기존 서비스 발견 — 중지 후 제거합니다.
    nssm stop %SERVICE_NAME% >nul 2>nul
    nssm remove %SERVICE_NAME% confirm >nul 2>nul
)

nssm install %SERVICE_NAME% "%CHROMA_EXE%" run --path "%CHROMA_PATH%" --host %HOST% --port %PORT% || (
    echo [error] nssm install 실패
    exit /b 1
)
nssm set %SERVICE_NAME% AppDirectory "%PROJECT_DIR%"
nssm set %SERVICE_NAME% AppStdout "%LOG_DIR%\chroma_service.log"
nssm set %SERVICE_NAME% AppStderr "%LOG_DIR%\chroma_service.err"
nssm set %SERVICE_NAME% AppRotateFiles 1
nssm set %SERVICE_NAME% AppRotateOnline 1
nssm set %SERVICE_NAME% AppRotateBytes 10485760
nssm set %SERVICE_NAME% Start SERVICE_AUTO_START
nssm set %SERVICE_NAME% AppExit Default Restart
nssm set %SERVICE_NAME% AppRestartDelay 5000
nssm set %SERVICE_NAME% Description "ChromaDB 서버 (인도네시아 법령 RAG, port %PORT%)"

echo.
echo [OK] 등록 완료. 다음 명령으로 관리:
echo   시작:        nssm start %SERVICE_NAME%
echo   중지:        nssm stop  %SERVICE_NAME%
echo   상태:        sc query   %SERVICE_NAME%
echo   재설치:      scripts\install_chroma_service.bat 다시 실행
echo   완전 제거:   nssm remove %SERVICE_NAME% confirm
echo.
echo 로그: %LOG_DIR%\chroma_service.log / .err
echo .env에 다음을 설정하면 rag_server / ingest 가 자동으로 이 서비스를 사용:
echo   RAG_CHROMA_MODE=http
echo   RAG_CHROMA_HOST=%HOST%
echo   RAG_CHROMA_PORT=%PORT%

endlocal
