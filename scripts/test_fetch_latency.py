"""Phase 9 진단 — fetch_article_chunks 단일 컬렉션 scan 시간 측정."""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import rag_server_v2 as r

cases = [
    ("UU_12_Tahun_2011_Pembentukan_Peraturan_Perundang-undangan.pdf", "Pasal 7"),
    ("UU_12_Tahun_2011_Pembentukan_Peraturan_Perundang-undangan.pdf", None),
    ("PP_19_Tahun_2024_Peraturan_Pelaksanaan_Undang-undang_Nomor_15_Tahun_1997_Tentang_Ketransmigrasian.pdf", "Pasal 1"),
    ("UUD_1945_원본.pdf", None),
]

for src, pasal in cases:
    cat = r._category_from_source(src)
    print(f"\n>>> source={src[:60]}... pasal={pasal}")
    print(f"    category_resolved={cat}")
    t0 = time.time()
    res = r.tool_fetch_article_chunks(src, pasal)
    elapsed = time.time() - t0
    print(f"    elapsed={elapsed:.2f}s, count={res.get('count')}, category_resolved_in_result={res.get('category_resolved')}")
