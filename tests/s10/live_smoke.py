"""Live Memo Check smoke test against real Telem. Not collected by pytest (no test_ prefix). Costs real money.

Usage: python tests/s10/live_smoke.py [memo_id] [--record]

Checks one example memo from ui/fixtures/memos.json end to end with a $0.40 cap,
prints the report, and fails unless at least one claim is VERIFIED with a real
source URL and the memo cost stays under $0.40. --record writes the finished
report to demo/memo_examples/<memo_id>.json as the UI's replay fallback.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from so import claims  # noqa: E402

CAP_USD = 0.40


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    memo_id = args[0] if args else "microsoft"
    memos = {m["id"]: m for m in json.loads((ROOT / "ui" / "fixtures" / "memos.json").read_text())["memos"]}
    memo = memos[memo_id]
    report = asyncio.run(claims.check_memo(memo["text"], max_cost_usd=CAP_USD))
    for c in report.claims:
        print(f"[{c.verdict:12}] {c.text}\n    why: {c.why}\n    source: {c.source_url}\n"
              f"    engines: {', '.join(c.engines_used)}  rescues: {c.rescues_used}  cost: ${c.cost_usd:.4f}")
    print(f"\ntotals: {report.totals.model_dump()}  engine_calls: {report.engine_calls}  fetches: {report.fetches}  "
          f"cost: ${report.cost_usd:.4f}  latency: {report.latency_ms / 1000:.1f}s  session: {report.telem_session_id}")
    for note in report.notes:
        print("note:", note)
    if "--record" in sys.argv:
        out = ROOT / "demo" / "memo_examples" / f"{memo_id}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = report.model_dump(mode="json")
        payload["recorded"] = {"memo_id": memo_id, "label": memo["label"], "source": "live run of tests/s10/live_smoke.py"}
        out.write_text(json.dumps(payload, indent=2) + "\n")
        print("recorded:", out.relative_to(ROOT))
    ok = any(c.verdict == "VERIFIED" and (c.source_url or "").startswith("http") for c in report.claims)
    ok = ok and report.cost_usd < CAP_USD
    print("SMOKE", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
