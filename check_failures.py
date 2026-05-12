"""실패 엔트리 (chunk_count=0) 분류 — oversize, missing, etc."""
import json
import os
import sys
from pathlib import Path
from collections import defaultdict

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

MANIFEST = Path(r"D:\rag_data\manifest.json")
MAX = 60 * 1024 * 1024  # MAX_PDF_BYTES default

with MANIFEST.open("r", encoding="utf-8") as f:
    data = json.load(f)

failed = [(p, e) for p, e in data.items() if e.get("chunk_count", 0) == 0]
print(f"실패 엔트리 총 {len(failed)}개")

oversize = []
missing = []
exists_normal = []
for p, e in failed:
    pp = Path(p)
    if not pp.exists():
        missing.append((p, e))
        continue
    sz = pp.stat().st_size
    if sz > MAX:
        oversize.append((p, sz, e))
    else:
        exists_normal.append((p, sz, e))

print(f"  - oversize (>60MB): {len(oversize)}")
print(f"  - missing (디스크에 없음): {len(missing)}")
print(f"  - 정상 크기인데 실패: {len(exists_normal)}")

# 카테고리별 분포
def by_cat(rows):
    d = defaultdict(int)
    for r in rows:
        e = r[-1]
        d[e.get("category", "?")] += 1
    return dict(d)

print()
print("oversize 카테고리:", by_cat(oversize))
print("정상크기 실패 카테고리:", by_cat(exists_normal))

# oversize sample
if oversize:
    print()
    print("oversize 상위 10:")
    for p, sz, e in sorted(oversize, key=lambda x: -x[1])[:10]:
        print(f"  {sz/1024/1024:6.1f}MB  {Path(p).name}")

# 정상크기 실패 sample (재시도 대상)
if exists_normal:
    print()
    print("재시도 대상 sample 10:")
    for p, sz, e in exists_normal[:10]:
        print(f"  {sz/1024/1024:5.1f}MB  [{e.get('category','?')}]  {Path(p).name}")
