"""ingest_loop를 stdio 직접 캡처로 실행하는 wrapper."""
import os, sys, datetime
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
log_path = LOG_DIR / f"ingest_w_{stamp}.log"
err_path = LOG_DIR / f"ingest_w_{stamp}.err"

# 파일을 직접 열어 sys.stdout/stderr 교체 (line buffered = 1)
sys.stdout = open(log_path, "w", encoding="utf-8", buffering=1, errors="replace")
sys.stderr = open(err_path, "w", encoding="utf-8", buffering=1, errors="replace")

# 자식 프로세스가 동일한 stdio를 상속하도록 OS-level FD 교체
os.dup2(sys.stdout.fileno(), 1)
os.dup2(sys.stderr.fileno(), 2)

# pointer 기록
with open(LOG_DIR / "ingest_w_latest.txt", "w") as f:
    f.write(f"{stamp}\n{log_path}\n{err_path}\n")

# 환경변수 (이미 부모에서 받음)
print(f"[wrapper] starting ingest_loop, log={log_path}", flush=True)

# delegate
sys.argv = ["ingest_loop.py", "--duration", "48h", "--interval", "60s"]
import runpy
runpy.run_path("ingest_loop.py", run_name="__main__")
