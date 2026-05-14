"""multi_query_retrieve 단계별 소요시간 프로파일링 (일회성 진단 스크립트)."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rag_server_v2 as rag  # noqa: E402

Q = "인도네시아 노동법상 최저임금 결정 절차는?"


def t(label, fn):
    s = time.perf_counter()
    out = fn()
    dt = time.perf_counter() - s
    print(f"  {label:32} {dt:7.2f}s")
    return out, dt


def main():
    print(f"질문: {Q}\n")

    # warmup embed model
    t("(warmup) encode_query x1", lambda: rag.encode_query(["warmup"]))
    print()

    analysis, _ = t("analyze_query (Haiku)", lambda: rag.analyze_query(Q))
    print(f"    -> sub_queries={len(analysis['sub_queries'])} id_keywords={len(analysis['id_keywords'])} hyde={len(analysis['hypothetical_id_answers'])}")

    # build query list (mirror of multi_query_retrieve)
    queries = [("original", Q)]
    for i, sq in enumerate(analysis["sub_queries"][:3]):
        if isinstance(sq, str) and sq.strip():
            queries.append((f"sub_{i+1}", sq.strip()))
    if analysis["id_keywords"]:
        queries.append(("id_keywords", " ".join(analysis["id_keywords"])))
    for i, hyde in enumerate(analysis["hypothetical_id_answers"][:2]):
        if isinstance(hyde, str) and hyde.strip():
            queries.append((f"hyde_{i+1}", hyde.strip()))
    if len(queries) > rag.MAX_EXPANDED_QUERIES:
        queries = queries[: rag.MAX_EXPANDED_QUERIES]
    print(f"    -> expanded queries = {len(queries)}")

    embeddings, _ = t(f"encode_query x{len(queries)}", lambda: rag.encode_query([q[1] for q in queries]))

    cols = rag.list_v2_collections()
    print(f"    -> collections = {len(cols)}")

    # single col.query timing
    emb0 = embeddings[0]
    _, single_dt = t("single col.query (1 col)", lambda: cols[0].query(query_embeddings=[emb0], n_results=30))

    # serial: all queries x all cols
    def _serial():
        n = 0
        for emb in embeddings:
            for col in cols:
                col.query(query_embeddings=[emb], n_results=30)
                n += 1
        return n
    n_serial, serial_dt = t("SERIAL all (q x col)", _serial)
    print(f"    -> {n_serial} queries serial")

    # parallel via ThreadPoolExecutor
    from concurrent.futures import ThreadPoolExecutor
    tasks = [(emb, col) for emb in embeddings for col in cols]

    def _one(task):
        emb, col = task
        return col.query(query_embeddings=[emb], n_results=30)

    def _par():
        with ThreadPoolExecutor(max_workers=rag.DENSE_RETRIEVAL_WORKERS) as ex:
            return list(ex.map(_one, tasks))
    _, par_dt = t(f"PARALLEL all ({rag.DENSE_RETRIEVAL_WORKERS} workers)", _par)
    print(f"    -> {len(tasks)} queries parallel")

    print()
    print(f"  speedup serial/parallel = {serial_dt / par_dt:.1f}x" if par_dt else "")
    print(f"  full multi_query_retrieve:")
    _, mqr_dt = t("multi_query_retrieve (end-to-end)", lambda: rag.multi_query_retrieve(Q, 3, None))


if __name__ == "__main__":
    main()
