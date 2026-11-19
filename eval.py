"""Measure the agent + MCP adapter on real tasks. Each task starts from a fresh Ledgerline database,
and success is checked in the database itself, not from what the agent says.

    uv run eval.py                 # all tasks
    uv run eval.py --only 3        # one task
    uv run eval.py --shots         # also save screenshots of every page the adapter touches
"""
import argparse
import asyncio
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import time
import urllib.request
from decimal import Decimal

ROOT = pathlib.Path(__file__).resolve().parent
DB = ROOT / "results" / "eval.db"
PORT = 8051

TASKS = [
    {"task": "Invoice Fenwick Builders for 2 site visits at £120 each, dated today.",
     "expect": {"new": [{"account": "FEN001", "total": "288.00", "date": "18/11/2026"}]}},
    {"task": "Bramble Kitchen: invoice dated 14/11/2026 for VAT return preparation £275 and 3 hours of payroll setup at £60 an hour.",
     "expect": {"new": [{"account": "BRA001", "total": "546.00", "date": "14/11/2026"}]}},
    {"task": "Raise an invoice to Harbour Street Opticians for one equipment calibration at £95, dated 1st October 2026.",
     "expect": {"new": [{"account": "HAR001", "total": "114.00", "date": "01/10/2026"}]}},
    {"task": "Invoice Kestrel for one month of bookkeeping at £620.",
     "expect": {"new": [], "answer_has_any": ["KES001", "Kestrel Logistics"], "answer_has_also": ["KES002", "Dental"]}},
    {"task": "Invoice Acme Plumbing for a boiler survey at £180.",
     "expect": {"new": [], "answer_has_any": ["not find", "can't find", "cannot find", "no customer", "No matching", "couldn't find", "could not find", "no match"]}},
    {"task": "Mill Road Motors needs one invoice for: MOT reminders setup £40, 2 x payroll run £45, CIS return £85, VAT review £150, "
             "bank reconciliation £95, credit control call £30, and management pack £220. Date it today.",
     "expect": {"new": [], "answer_has_any": ["5 lines", "five lines", "at most 5", "maximum of 5", "up to 5"]}},
    {"task": "Mill Road Motors: invoice today for MOT reminders setup £40, 2 x payroll run £45, CIS return £85, VAT review £150, "
             "bank reconciliation £95, credit control call £30, and management pack £220. Split it across invoices if the system needs you to.",
     "expect": {"new_total": {"account": "MIL001", "count": 2, "total": "852.00"}}},
    {"task": "How much does Kestrel Logistics owe us at the moment?",
     "expect": {"new": [], "answer_has_any": ["1,704", "1704"]}},
    {"task": "What were our total sales including VAT in June 2026, and how many invoices?",
     "expect": {"new": [], "answer_has_any": ["1,818", "1818"], "answer_has_also": ["3"]}},
    {"task": "Which customer had the largest single invoice in July 2026, and for how much including VAT?",
     "expect": {"new": [], "answer_has_any": ["Mill Road"], "answer_has_also": ["1,740", "1740"]}},
    {"task": "What was billed on invoice INV-01043?",
     "expect": {"new": [], "answer_has_any": ["Payroll"], "answer_has_also": ["960"]}},
    {"task": "Invoice Ashworth & Daughters for 1 x joinery quote review at £0.",
     "expect": {"new": [], "answer_has_any": ["£0", "zero", "positive", "price", "0.00"]}},
    {"task": "Invoice Wensum Valley Garden Centre for 1 annual accounts at £980 dated 31/11/2026.",
     "expect": {"new": [], "answer_has_any": ["31/11", "not a valid", "invalid", "doesn't exist", "does not exist", "30 days", "November has"]}},
]


def reset_db():
    DB.parent.mkdir(exist_ok=True)
    env = {**os.environ, "LEDGERLINE_DB": str(DB)}
    subprocess.run([sys.executable, "-c", "from legacy_app.app import init_db; init_db(reset=True)"], cwd=ROOT, env=env, check=True)


def invoices():
    con = sqlite3.connect(DB)
    rows = con.execute("select number, account, date, total from invoices order by number").fetchall()
    con.close()
    return [{"number": r[0], "account": r[1], "date": r[2], "total": r[3]} for r in rows]


def check(expect, new, answer):
    a = answer.lower()
    fails = []
    if "new" in expect:
        want = expect["new"]
        if len(new) != len(want):
            fails.append(f"expected {len(want)} new invoice(s), got {len(new)}")
        for w, n in zip(want, new):
            for k, v in w.items():
                if n[k] != v:
                    fails.append(f"{k}: expected {v}, got {n[k]}")
    if "new_total" in expect:
        e = expect["new_total"]
        if len(new) != e["count"] or any(n["account"] != e["account"] for n in new):
            fails.append(f"expected {e['count']} invoices for {e['account']}, got {[(n['account']) for n in new]}")
        tot = sum((Decimal(n["total"]) for n in new), Decimal("0"))
        if str(tot) != e["total"]:
            fails.append(f"total: expected {e['total']}, got {tot}")
    if "answer_has_any" in expect and not any(s.lower() in a for s in expect["answer_has_any"]):
        fails.append(f"answer lacks any of {expect['answer_has_any']}")
    if "answer_has_also" in expect and not any(s.lower() in a for s in expect["answer_has_also"]):
        fails.append(f"answer lacks any of {expect['answer_has_also']}")
    return fails


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", type=int)
    ap.add_argument("--shots", action="store_true")
    ap.add_argument("--idle", default="20", help="Ledgerline session timeout in seconds (short, to exercise re-login)")
    a = ap.parse_args()
    sys.path.insert(0, str(ROOT))
    from agent import MODEL, run

    reset_db()
    app_env = {**os.environ, "LEDGERLINE_DB": str(DB), "LEDGERLINE_IDLE_SECONDS": a.idle}
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "legacy_app.app:app", "--port", str(PORT), "--log-level", "warning"],
                              cwd=ROOT, env=app_env)
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/")
                break
            except OSError:
                time.sleep(0.2)
        rows = []
        for i, t in enumerate(TASKS, 1):
            if a.only and i != a.only:
                continue
            reset_db()
            before = {r["number"] for r in invoices()}
            senv = {"LEDGERLINE_URL": f"http://127.0.0.1:{PORT}"}
            if a.shots:
                senv["SCREENSHOTS"] = str(ROOT / "results" / "screenshots" / f"task{i:02d}")
            t0 = time.time()
            print(f"\n=== Task {i}: {t['task']}")
            out = await run(t["task"], log=lambda s: print("   ", s[:200]), server_env=senv)
            new = [r for r in invoices() if r["number"] not in before]
            fails = check(t["expect"], new, out["answer"])
            print(f"   answer: {out['answer'][:300]}")
            print(f"   {'PASS' if not fails else 'FAIL ' + '; '.join(fails)}")
            rows.append({"n": i, "task": t["task"], "pass": not fails, "fails": fails, "answer": out["answer"],
                         "calls": [{"tool": c[0], "input": c[1], "output": c[2][:500]} for c in out["calls"]],
                         "new_invoices": new, "seconds": round(time.time() - t0, 1), "usage": out["usage"]})
        summary = {"model": MODEL, "tasks": len(rows), "passed": sum(r["pass"] for r in rows),
                   "tool_calls": sum(len(r["calls"]) for r in rows),
                   "avg_seconds": round(sum(r["seconds"] for r in rows) / max(len(rows), 1), 1),
                   "input_tokens": sum(r["usage"]["input"] for r in rows),
                   "output_tokens": sum(r["usage"]["output"] for r in rows)}
        print("\n" + json.dumps(summary, indent=2))
        (ROOT / "results").mkdir(exist_ok=True)
        if not a.only:
            (ROOT / "results" / "eval.json").write_text(json.dumps({"summary": summary, "tasks": rows}, indent=2, ensure_ascii=False))
    finally:
        server.terminate()


if __name__ == "__main__":
    asyncio.run(main())
