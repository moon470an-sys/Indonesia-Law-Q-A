"""
v1 → v2 atomic switch.

작업:
1. watchdog.ps1 의 uvicorn 명령을 rag_server:app → rag_server_v2:app 로 교체.
2. 기존 uvicorn 프로세스 kill → watchdog가 자동으로 v2를 8000 포트에 띄움.

원복:
    --revert 플래그로 rag_server:app 으로 되돌림.

⚠️ 운영 영향: watchdog grace period(2.5분) + v2 startup(BGE-M3 cold load ~5~10분) = 다운타임 발생.
사용자 동의 후 실행.
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WATCHDOG = ROOT / "auto_start" / "watchdog.ps1"

# 정규식: PowerShell array literal `"rag_server:app"` 매치
PATTERN_V1 = re.compile(r'(["\'])rag_server:app\1')
PATTERN_V2 = re.compile(r'(["\'])rag_server_v2:app\1')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--revert", action="store_true", help="v1으로 되돌림")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not WATCHDOG.exists():
        print(f"FATAL: {WATCHDOG} 없음")
        return 1

    content = WATCHDOG.read_text(encoding="utf-8")

    if args.revert:
        target_from = PATTERN_V2
        target_to = '"rag_server:app"'
        label = "v2 → v1"
    else:
        target_from = PATTERN_V1
        target_to = '"rag_server_v2:app"'
        label = "v1 → v2"

    new_content, n = target_from.subn(target_to, content)
    if n == 0:
        print(f"치환 대상 없음 (이미 적용됐거나 패턴 변경?). label={label}")
        return 0
    print(f"치환 {n}건: {label}")

    if args.dry_run:
        print("--- diff preview (first 80 chars per change) ---")
        for m in target_from.finditer(content):
            start = max(0, m.start() - 40)
            end = min(len(content), m.end() + 40)
            print(f"  context: ...{content[start:end]}...")
        return 0

    # backup
    bak = WATCHDOG.with_suffix(".ps1.bak")
    shutil.copy2(WATCHDOG, bak)
    print(f"  backup: {bak}")

    WATCHDOG.write_text(new_content, encoding="utf-8")
    print(f"  rewrote: {WATCHDOG}")
    print("\n다음 단계 (수동):")
    print("  1. uvicorn 8000 프로세스 kill → watchdog 자동 재시작")
    print("     Stop-Process -Id (Get-NetTCPConnection -LocalPort 8000 -State Listen).OwningProcess -Force")
    print("  2. watchdog가 새 명령으로 띄움. BGE-M3 cold load 5~10분 + prewarm 후 정상화.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
