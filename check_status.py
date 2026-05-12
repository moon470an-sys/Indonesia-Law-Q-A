"""manifest + 디스크 PDF 비교해서 outstanding 작업 파악."""
import json
import os
import sys
from pathlib import Path
from collections import defaultdict

# UTF-8 콘솔
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

MANIFEST = Path(r"D:\rag_data\manifest.json")
SOURCE = Path(r"D:\인도네시아 법령 원문")

with MANIFEST.open("r", encoding="utf-8") as f:
    data = json.load(f)

print(f"Manifest 파일: {MANIFEST}")
print(f"Manifest entries (총): {len(data):,}")

# 카테고리별 집계
total_by_cat = defaultdict(int)
ok_by_cat = defaultdict(int)
failed_by_cat = defaultdict(int)
total_chunks = 0
for path, e in data.items():
    cat = e.get("category", "?")
    total_by_cat[cat] += 1
    cc = e.get("chunk_count", 0)
    if cc == 0:
        failed_by_cat[cat] += 1
    else:
        ok_by_cat[cat] += 1
        total_chunks += cc

# 디스크 PDF 카운트
disk_by_cat = defaultdict(int)
disk_paths_by_cat = defaultdict(set)
for entry in sorted(SOURCE.iterdir()):
    if not entry.is_dir() or entry.name.startswith((".", "__")):
        continue
    cnt = 0
    pset = set()
    for pdf in entry.rglob("*.pdf"):
        cnt += 1
        pset.add(str(pdf))
    disk_by_cat[entry.name] = cnt
    disk_paths_by_cat[entry.name] = pset

print()
print(f"{'카테고리':<22} {'디스크':>7} {'manifest':>9} {'OK':>7} {'failed':>7} {'미인덱스':>9}")
print("-" * 70)
all_cats = sorted(set(disk_by_cat) | set(total_by_cat))
total_disk = total_man = total_ok = total_failed = total_pending = 0
for cat in all_cats:
    d = disk_by_cat.get(cat, 0)
    m = total_by_cat.get(cat, 0)
    ok = ok_by_cat.get(cat, 0)
    failed = failed_by_cat.get(cat, 0)
    pending = d - m  # manifest에 아직 안 등록된 것
    print(f"{cat:<22} {d:>7,} {m:>9,} {ok:>7,} {failed:>7,} {pending:>9,}")
    total_disk += d; total_man += m; total_ok += ok; total_failed += failed; total_pending += pending
print("-" * 70)
print(f"{'TOTAL':<22} {total_disk:>7,} {total_man:>9,} {total_ok:>7,} {total_failed:>7,} {total_pending:>9,}")
print()
print(f"OK 청크 합계 (manifest 기준): {total_chunks:,}")
print(f"미인덱스 PDF (디스크 - manifest): {total_pending:,}")
print(f"실패 엔트리 재시도 대상: {sum(failed_by_cat.values()):,}")

# manifest에는 있지만 디스크엔 없는 것 (delete 대상)
manifest_paths = set(data.keys())
all_disk_paths = set()
for s in disk_paths_by_cat.values():
    all_disk_paths |= s
ghost = manifest_paths - all_disk_paths
print(f"디스크에 없는 manifest entry (삭제 대상): {len(ghost):,}")
