"""Adapter tests against a real Ledgerline instance (no model, no API keys)."""
import os
import pathlib
import subprocess
import sys
import time
import urllib.request

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PORT = 8052


@pytest.fixture(scope="module")
def app_url(tmp_path_factory):
    db = tmp_path_factory.mktemp("ll") / "test.db"
    env = {**os.environ, "LEDGERLINE_DB": str(db), "LEDGERLINE_IDLE_SECONDS": "1"}
    subprocess.run([sys.executable, "-c", "from legacy_app.app import init_db; init_db(reset=True)"], cwd=ROOT, env=env, check=True)
    p = subprocess.Popen([sys.executable, "-m", "uvicorn", "legacy_app.app:app", "--port", str(PORT), "--log-level", "warning"],
                         cwd=ROOT, env=env)
    url = f"http://127.0.0.1:{PORT}"
    for _ in range(50):
        try:
            urllib.request.urlopen(url)
            break
        except OSError:
            time.sleep(0.2)
    yield url
    p.terminate()


@pytest.fixture(scope="module")
def ledger(app_url):
    os.environ["LEDGERLINE_URL"] = app_url
    import importlib
    import adapter.ledgerline as mod
    importlib.reload(mod)
    l = mod.Ledgerline()
    yield l, mod
    l.close()


def test_find_customer_returns_all_matches(ledger):
    l, _ = ledger
    found = l.find_customer("kestrel")
    assert [c["account"] for c in found] == ["KES001", "KES002"]
    assert found[0]["balance"] == "1704.00"
    assert l.find_customer("acme") == []


def test_create_invoice_is_verified(ledger):
    l, _ = ledger
    res = l.create_invoice("bra001", [{"description": "VAT return", "quantity": 1, "unit_price": 275},
                                      {"description": "Payroll setup", "quantity": 3, "unit_price": "60"}], "14/11/2026")
    assert res["verified"] and res["total"] == "546.00" and res["account"] == "BRA001"
    inv = l.get_invoice(res["invoice_number"])
    assert [x["description"] for x in inv["lines"]] == ["VAT return", "Payroll setup"]


@pytest.mark.parametrize("account,items,date,msg", [
    ("Bramble", [{"description": "x", "quantity": 1, "unit_price": 1}], None, "not an account code"),
    ("ZZZ999", [{"description": "x", "quantity": 1, "unit_price": 1}], None, "not found"),
    ("BRA001", [{"description": "x", "quantity": 1, "unit_price": 0}], None, "positive"),
    ("BRA001", [{"description": "x", "quantity": 1, "unit_price": 1}] * 6, None, "at most 5"),
    ("BRA001", [{"description": "x", "quantity": 1, "unit_price": 1}], "31/11/2026", "DD/MM/YYYY"),
])
def test_bad_requests_are_explained_not_posted(ledger, account, items, date, msg):
    l, mod = ledger
    before = len(l.list_invoices())
    with pytest.raises(mod.LedgerlineError, match=msg):
        l.create_invoice(account, items, date)
    assert len(l.list_invoices()) == before


def test_recovers_from_session_expiry(ledger):
    l, _ = ledger
    l.find_customer("fenwick")
    time.sleep(2)  # the app's idle timeout is 1 second in this test
    assert l.find_customer("fenwick")[0]["account"] == "FEN001"
    assert any("session expired" in e for e in l.log[1:])


def test_daybook_export(ledger):
    l, _ = ledger
    rep = l.export_sales_daybook("01/06/2026", "30/06/2026")
    assert rep["count"] == 3 and rep["total"] == "1818.00"
