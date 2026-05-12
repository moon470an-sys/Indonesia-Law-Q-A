"""Anthropic Admin API로 누적 사용량/비용 조회.

크레딧 잔액(credit balance) endpoint는 Anthropic이 공개 API로 제공하지 않음
— https://console.anthropic.com/settings/billing 에서만 확인 가능.

본 스크립트는 그 차선책으로:
  1) 이번 달 총 비용 (USD, cost_report)
  2) 일별 비용 추이
  3) 모델/토큰 타입별 비용 breakdown
  4) 모델별 토큰 누계 (uncached input / cache read / cache creation / output)

준비:
  - Console → Settings → Admin Keys → "Create Admin Key" 로 별도 키 발급
    (일반 ANTHROPIC_API_KEY와 다른, sk-ant-admin01-... 형식)
  - .env에 ANTHROPIC_ADMIN_API_KEY=sk-ant-admin01-... 추가
  - 관리자(owner/admin) 권한 필요

사용:
  PYTHONIOENCODING=utf-8 python scripts/anthropic_usage.py
  PYTHONIOENCODING=utf-8 python scripts/anthropic_usage.py --days 30
  PYTHONIOENCODING=utf-8 python scripts/anthropic_usage.py --since 2026-05-01
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

API_BASE = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"


def _load_env() -> None:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _api_get(path: str, params: dict, key: str) -> dict:
    url = f"{API_BASE}{path}?{urllib.parse.urlencode(params, doseq=True)}"
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise SystemExit(
            f"HTTP {e.code} on {path}\n{body}\n"
            "→ Admin API key가 맞는지(관리자 권한) Console에서 확인하세요."
        )


def _paginate(path: str, params: dict, key: str) -> list[dict]:
    """has_more=True면 next_page로 계속 가져옴. 모든 data 버킷 concat."""
    out: list[dict] = []
    cursor = None
    for _ in range(20):  # 안전한 상한
        p = dict(params)
        if cursor:
            p["page"] = cursor
        resp = _api_get(path, p, key)
        out.extend(resp.get("data") or [])
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_page")
        if not cursor:
            break
    return out


def _month_start_utc() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _to_rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def cost_report(key: str, starting_at: str, group_by: list[str] | None = None) -> list[dict]:
    params: dict = {"starting_at": starting_at, "bucket_width": "1d", "limit": 31}
    if group_by:
        params["group_by[]"] = group_by
    return _paginate("/v1/organizations/cost_report", params, key)


def messages_usage(key: str, starting_at: str, group_by: list[str] | None = None) -> list[dict]:
    params: dict = {"starting_at": starting_at, "bucket_width": "1d", "limit": 31}
    if group_by:
        params["group_by[]"] = group_by
    return _paginate("/v1/organizations/usage_report/messages", params, key)


def _fmt_usd(cents: float) -> str:
    return f"${cents / 100:.4f}"


def _print_section(title: str) -> None:
    print()
    print(title)
    print("-" * len(title))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--since", help="조회 시작일 YYYY-MM-DD (기본: 이번 달 1일 UTC)")
    parser.add_argument("--days", type=int, help="오늘로부터 N일 전부터 조회 (--since 우선)")
    parser.add_argument("--json", action="store_true", help="raw JSON으로 덤프")
    args = parser.parse_args()

    _load_env()
    key = os.environ.get("ANTHROPIC_ADMIN_API_KEY")
    if not key:
        sys.exit(
            "ANTHROPIC_ADMIN_API_KEY가 환경 변수/.env에 없습니다.\n"
            "Console → Settings → Admin Keys → Create Admin Key 로 발급 후\n"
            ".env에 추가:  ANTHROPIC_ADMIN_API_KEY=sk-ant-admin01-...\n"
            "(일반 ANTHROPIC_API_KEY와 별도 키입니다.)"
        )

    if args.since:
        start_dt = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
    elif args.days:
        start_dt = datetime.now(timezone.utc) - timedelta(days=args.days)
        start_dt = start_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        start_dt = _month_start_utc()
    starting = _to_rfc3339(start_dt)

    if args.json:
        out = {
            "starting_at": starting,
            "cost_total": cost_report(key, starting),
            "cost_by_description": cost_report(key, starting, group_by=["description"]),
            "usage_by_model": messages_usage(key, starting, group_by=["model"]),
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return

    print(f"=== Anthropic 사용량 — {starting} 이후 (UTC) ===")
    print(f"※ 크레딧 잔액은 API 미제공. https://console.anthropic.com/settings/billing 에서 확인.")

    # 1) 일별 총 비용
    buckets = cost_report(key, starting)
    daily: list[tuple[str, float]] = []
    grand_cents = 0.0
    for b in buckets:
        date = (b.get("starting_at") or "")[:10]
        cents = sum(float(r.get("amount", 0) or 0) for r in (b.get("results") or []))
        grand_cents += cents
        daily.append((date, cents))

    _print_section(f"총 비용 (이 기간): {_fmt_usd(grand_cents)} ({len(daily)}일)")
    for date, cents in daily:
        bar = "█" * min(40, int(cents / max(grand_cents / 40, 1))) if grand_cents > 0 else ""
        print(f"  {date}  {_fmt_usd(cents):>12s}  {bar}")

    # 2) 모델/토큰 타입별 비용
    buckets_desc = cost_report(key, starting, group_by=["description"])
    model_cost: dict[str, float] = {}
    for b in buckets_desc:
        for r in (b.get("results") or []):
            cost_type = r.get("cost_type") or ""
            model = r.get("model") or ""
            token_type = r.get("token_type") or ""
            if cost_type == "tokens":
                label = f"{model} / {token_type}"
            else:
                label = f"({cost_type})"
            cents = float(r.get("amount", 0) or 0)
            if cents <= 0:
                continue
            model_cost[label] = model_cost.get(label, 0.0) + cents

    _print_section("모델/토큰 타입별 비용 (이 기간)")
    if not model_cost:
        print("  (데이터 없음)")
    else:
        for label, cents in sorted(model_cost.items(), key=lambda kv: -kv[1]):
            pct = (cents / grand_cents * 100) if grand_cents > 0 else 0
            print(f"  {label:<70s}  {_fmt_usd(cents):>12s}  ({pct:5.1f}%)")

    # 3) 모델별 토큰 누계
    usage_buckets = messages_usage(key, starting, group_by=["model"])
    model_tok: dict[str, dict[str, int]] = {}
    for b in usage_buckets:
        for r in (b.get("results") or []):
            m = r.get("model") or "(unknown)"
            cc = r.get("cache_creation") or {}
            t = model_tok.setdefault(m, {"input_uncached": 0, "cache_read": 0,
                                          "cache_creation": 0, "output": 0})
            t["input_uncached"] += int(r.get("uncached_input_tokens") or 0)
            t["cache_read"] += int(r.get("cache_read_input_tokens") or 0)
            t["cache_creation"] += (int(cc.get("ephemeral_5m_input_tokens") or 0)
                                     + int(cc.get("ephemeral_1h_input_tokens") or 0))
            t["output"] += int(r.get("output_tokens") or 0)

    _print_section("모델별 토큰 누계 (이 기간)")
    if not model_tok:
        print("  (데이터 없음)")
    else:
        for m, t in sorted(model_tok.items()):
            total_in = t["input_uncached"] + t["cache_read"] + t["cache_creation"]
            cache_hit_pct = (t["cache_read"] / total_in * 100) if total_in > 0 else 0
            print(f"  {m}")
            print(f"    uncached input : {t['input_uncached']:>14,}")
            print(f"    cache read     : {t['cache_read']:>14,}   ({cache_hit_pct:5.1f}% of input)")
            print(f"    cache creation : {t['cache_creation']:>14,}")
            print(f"    output         : {t['output']:>14,}")
            print(f"    total input    : {total_in:>14,}")


if __name__ == "__main__":
    main()
