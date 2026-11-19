"""Adapter: operates Ledgerline Accounts through its web pages, the way a member of staff would.

Each public method is one complete task. Navigation, form filling, session recovery and
checking the result all happen here, so the AI only ever sees meaningful operations and
structured results, never pages or selectors.
"""
import csv
import io
import os
import pathlib
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from playwright.sync_api import sync_playwright

BASE = os.environ.get("LEDGERLINE_URL", "http://127.0.0.1:8050")
USER = os.environ.get("LEDGERLINE_USER", "accounts")
PASSWORD = os.environ.get("LEDGERLINE_PASSWORD", "ledger2004")
MAX_LINES = 5


class LedgerlineError(Exception):
    """A problem the AI should see and can act on (bad account code, invalid line, and so on)."""


def _money(s):
    return Decimal(s.replace("£", "").replace(",", "").strip())


class Ledgerline:
    def __init__(self, headless=True, screenshots=None, slow_mo=0):
        self._pw = sync_playwright().start()
        channel = os.environ.get("BROWSER_CHANNEL", "chrome")  # use the installed Chrome; no browser download
        self.browser = self._pw.chromium.launch(channel=channel, headless=headless, slow_mo=slow_mo)
        self.page = self.browser.new_page(viewport={"width": 1100, "height": 640})
        self.shots = pathlib.Path(screenshots) if screenshots else None
        self.shot_n = 0
        self.log = []  # every page action, for the audit trail

    def close(self):
        self.browser.close()
        self._pw.stop()

    # ------------------------------------------------------------ plumbing

    def _shot(self, name):
        if self.shots:
            self.shots.mkdir(parents=True, exist_ok=True)
            self.shot_n += 1
            self.page.screenshot(path=str(self.shots / f"{self.shot_n:02d}-{name}.png"))

    def _login(self):
        self.page.goto(f"{BASE}/")
        self.page.fill("input[name=u]", USER)
        self.page.fill("input[name=p]", PASSWORD)
        self._shot("login")
        self.page.click("input[type=submit][value='Log on']")
        if "/main" not in self.page.url:
            raise LedgerlineError("Log on to Ledgerline failed: check the service account credentials.")
        self.log.append("logged on")

    def _open(self, path):
        """Go to a page, logging on again if the session has expired (the app redirects to /?expired=1)."""
        self.page.goto(f"{BASE}{path}")
        if self.page.url.rstrip("/").endswith(BASE.split("//")[1]) or "expired=1" in self.page.url or "/?" in self.page.url:
            self.log.append("not logged on or session expired: logging on")
            self._login()
            self.page.goto(f"{BASE}{path}")

    def _rows(self, table="table.grid"):
        rows = []
        for tr in self.page.locator(f"{table} tr").all()[1:]:
            cells = [c.inner_text().strip() for c in tr.locator("td").all()]
            if cells:
                rows.append(cells)
        return rows

    # ------------------------------------------------------------ tasks

    def find_customer(self, name):
        """Search customers by name or account code. Returns every match with its balance."""
        if not name or not name.strip():
            raise LedgerlineError("Give part of the customer's name or account code.")
        self._open("/customers")
        self.page.fill("input[name=q]", name.strip())
        self.page.click("input[type=submit][value='Find']")
        self._shot("customer-search")
        out = []
        for cells in self._rows():
            if len(cells) == 4:
                out.append({"account": cells[0], "name": cells[1], "town": cells[2], "balance": str(_money(cells[3]))})
        self.log.append(f"find_customer {name!r}: {len(out)} match(es)")
        return out

    def create_invoice(self, account, items, date=None):
        """Post a sales invoice and confirm it exists with the expected total before returning."""
        account = (account or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{3}\d{3}", account):
            raise LedgerlineError(f"'{account}' is not an account code (e.g. FEN001). Use find_customer first.")
        if not items:
            raise LedgerlineError("An invoice needs at least one line.")
        if len(items) > MAX_LINES:
            raise LedgerlineError(f"Ledgerline accepts at most {MAX_LINES} lines per invoice; split it into two invoices.")
        clean = []
        for i, it in enumerate(items, 1):
            try:
                qty = int(it["quantity"])
                unit = Decimal(str(it["unit_price"])).quantize(Decimal("0.01"))
            except (KeyError, ValueError, InvalidOperation):
                raise LedgerlineError(f"Line {i}: needs description, a whole-number quantity and a unit price.") from None
            desc = str(it.get("description", "")).strip()
            if not desc or qty <= 0 or unit <= 0:
                raise LedgerlineError(f"Line {i}: needs description, a positive quantity and a positive unit price.")
            clean.append((desc[:60], qty, unit))
        date = date or datetime.now().strftime("%d/%m/%Y")
        try:
            datetime.strptime(date, "%d/%m/%Y")
        except ValueError:
            raise LedgerlineError("Date must be DD/MM/YYYY.") from None

        self._open("/invoice/new")
        self.page.fill("input[name=ac]", account)
        self.page.fill("input[name=dt]", date)
        for i, (d, q, u) in enumerate(clean, 1):
            self.page.fill(f"input[name=d{i}]", d)
            self.page.fill(f"input[name=q{i}]", str(q))
            self.page.fill(f"input[name=u{i}]", f"{u:.2f}")
        self._shot("invoice-entry")
        self.page.click("input[type=submit][value='Post']")
        self._shot("invoice-posted")

        err = self.page.locator("p.err")
        if err.count():
            raise LedgerlineError(f"Ledgerline rejected the invoice: {err.first.inner_text().strip()}")
        ok = self.page.locator("p.ok")
        m = re.search(r"Invoice (INV-\d+) posted to (\w+)", ok.first.inner_text() if ok.count() else "")
        if not m:
            raise LedgerlineError("Could not confirm the invoice was posted. Check the invoice list before retrying.")
        number = m.group(1)

        # Verify: open the posted invoice and check account, date, every line and the total match what we sent.
        inv = self.get_invoice(number)
        net = sum((q * u for _, q, u in clean), Decimal("0"))
        expected = net + (net * Decimal("0.20")).quantize(Decimal("0.01"))
        saved = [(l["description"], l["quantity"], Decimal(l["unit_price"])) for l in inv["lines"]]
        if (inv["account"] != account or inv["date"] != date or saved != clean
                or Decimal(inv["total"] or "0") != expected):
            raise LedgerlineError(f"Invoice {number} was posted but does not match the request; "
                                  "check it in Ledgerline before posting it again.")
        self.log.append(f"create_invoice {account}: {number} total {expected}")
        return {"invoice_number": number, "account": account, "date": date,
                "net": str(net), "vat": inv["vat"], "total": inv["total"], "verified": True}

    def get_invoice(self, number):
        number = (number or "").strip().upper()
        self._open(f"/invoice/view?n={number}")
        if self.page.locator("p.err").count():
            raise LedgerlineError(f"Invoice {number} not found.")
        self._shot("invoice-view")
        head = self.page.locator("fieldset").first.inner_text()
        m_ac, m_dt = re.search(r"A/C\s+(\w+)", head), re.search(r"Date\s+([\d/]+)", head)
        if not (m_ac and m_dt):
            raise LedgerlineError(f"Could not read invoice {number}; the page layout may have changed.")
        account, date = m_ac.group(1), m_dt.group(1)
        lines, totals = [], {}
        for cells in self._rows():
            if len(cells) == 5:
                lines.append({"description": cells[1], "quantity": int(cells[2]),
                              "unit_price": str(_money(cells[3])), "amount": str(_money(cells[4]))})
            elif len(cells) == 2:
                totals[cells[0].lower()] = str(_money(cells[1]))
        return {"invoice_number": number, "account": account, "date": date, "lines": lines,
                "net": totals.get("net"), "vat": totals.get("vat"), "total": totals.get("total")}

    def list_invoices(self, account=None):
        self._open("/invoices" + (f"?ac={account.strip().upper()}" if account else ""))
        self._shot("invoice-list")
        return [{"invoice_number": c[0], "account": c[1], "date": c[2], "total": str(_money(c[3]))}
                for c in self._rows() if len(c) == 4]

    def export_sales_daybook(self, start_date, end_date):
        """Run the Sales day book report and return its rows (the app only offers a CSV download)."""
        for d in (start_date, end_date):
            try:
                datetime.strptime(d, "%d/%m/%Y")
            except (TypeError, ValueError):
                raise LedgerlineError("Dates must be DD/MM/YYYY.") from None
        self._open("/reports")
        self.page.fill("input[name=f]", start_date)
        self.page.fill("input[name=t]", end_date)
        self._shot("report")
        with self.page.expect_download() as dl:
            self.page.click("input[type=submit][value='Export CSV']")
        text = pathlib.Path(dl.value.path()).read_text()
        rows = list(csv.DictReader(io.StringIO(text)))
        total = sum((Decimal(r["Total"]) for r in rows), Decimal("0"))
        self.log.append(f"export_sales_daybook {start_date}-{end_date}: {len(rows)} rows")
        return {"rows": rows, "count": len(rows), "total": str(total), "file": dl.value.suggested_filename}
