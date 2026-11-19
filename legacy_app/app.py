"""Ledgerline Accounts 4.2: a fictional legacy accounting system, built to look and behave like the real thing.

No API, no JSON. Server-rendered pages, table layouts, unlabelled inputs, account codes typed by hand,
dd/mm/yyyy dates, a session that expires when idle, and reports that only export as CSV.

    uv run uvicorn legacy_app.app:app --port 8050
"""
import csv
import html
import io
import os
import pathlib
import secrets
import sqlite3
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

DB = pathlib.Path(os.environ.get("LEDGERLINE_DB", pathlib.Path(__file__).parent / "ledgerline.db"))
IDLE_SECONDS = int(os.environ.get("LEDGERLINE_IDLE_SECONDS", "300"))
USERS = {"accounts": "ledger2004"}
VAT = Decimal("0.20")

CUSTOMERS = [
    ("FEN001", "Fenwick Builders Ltd", "Norwich"),
    ("KES001", "Kestrel Logistics Ltd", "Ipswich"),
    ("KES002", "Kestrel Lane Dental Practice", "Cambridge"),
    ("BRA001", "Bramble Kitchen", "Norwich"),
    ("ASH001", "Ashworth & Daughters Joinery", "King's Lynn"),
    ("WEN001", "Wensum Valley Garden Centre", "Dereham"),
    ("HAR001", "Harbour Street Opticians", "Great Yarmouth"),
    ("MIL001", "Mill Road Motors", "Cambridge"),
]
SEED_INVOICES = [  # account, date, [(description, qty, unit price)]
    ("FEN001", "03/06/2026", [("Site survey", 1, "450.00"), ("Drawings revision", 2, "85.00")]),
    ("KES001", "11/06/2026", [("Monthly bookkeeping - May", 1, "620.00")]),
    ("BRA001", "18/06/2026", [("VAT return preparation", 1, "275.00")]),
    ("KES001", "02/07/2026", [("Monthly bookkeeping - June", 1, "620.00"), ("Payroll run", 4, "45.00")]),
    ("MIL001", "15/07/2026", [("Year-end accounts", 1, "1450.00")]),
]

SESSIONS = {}  # token -> (user, last_seen)

STYLE = """
body{margin:0;background:#d4d0c8;font:11px Tahoma,Verdana,Arial,sans-serif;color:#000}
.title{background:linear-gradient(#0a246a,#3a6ea5);color:#fff;font-weight:bold;padding:4px 8px;font-size:12px}
.title span{float:right;font-weight:normal}
table.frame{width:100%;border-collapse:collapse}
td.nav{width:150px;vertical-align:top;background:#ece9d8;border-right:1px solid #808080;padding:6px}
td.nav a{display:block;color:#00c;padding:2px 0}
td.main{vertical-align:top;padding:10px}
fieldset{border:1px solid #808080;padding:8px;margin:0 0 8px;background:#ece9d8}
legend{font-weight:bold}
table.grid{border-collapse:collapse;background:#fff}
table.grid td,table.grid th{border:1px solid #808080;padding:2px 6px}
table.grid th{background:#ece9d8;text-align:left}
input[type=text],input[type=password]{font:11px Tahoma;border:1px solid #7f9db9;padding:1px 2px}
input[type=submit]{font:11px Tahoma;background:#ece9d8;border:1px outset #fff;padding:2px 10px}
.err{color:#c00;font-weight:bold}
.ok{color:#060;font-weight:bold}
.status{background:#ece9d8;border-top:1px solid #808080;padding:2px 8px;position:fixed;bottom:0;left:0;right:0}
"""


# ------------------------------------------------------------------ data

def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def money(d):
    return f"£{Decimal(d):,.2f}"


def init_db(reset=False):
    if reset and DB.exists():
        DB.unlink()
    con = db()
    con.executescript("""
    create table if not exists customers (account text primary key, name text, town text);
    create table if not exists invoices (number text primary key, account text, date text, net text, vat text, total text, posted_at real);
    create table if not exists lines (invoice text, n int, description text, qty int, unit text, amount text);
    """)
    if not con.execute("select count(*) from customers").fetchone()[0]:
        con.executemany("insert into customers values (?, ?, ?)", CUSTOMERS)
        for acc, date, items in SEED_INVOICES:
            post_invoice(con, acc, date, [(d, q, Decimal(u)) for d, q, u in items])
    con.commit()
    con.close()


def next_number(con):
    n = con.execute("select count(*) from invoices").fetchone()[0]
    return f"INV-{1040 + n:05d}"


def post_invoice(con, account, date, items):
    number = next_number(con)
    net = sum((q * u for _, q, u in items), Decimal("0"))
    vat = (net * VAT).quantize(Decimal("0.01"))
    con.execute("insert into invoices values (?, ?, ?, ?, ?, ?, ?)",
                (number, account, date, str(net), str(vat), str(net + vat), time.time()))
    con.executemany("insert into lines values (?, ?, ?, ?, ?, ?)",
                    [(number, i + 1, d, q, str(u), str(q * u)) for i, (d, q, u) in enumerate(items)])
    return number


def balance(con, account):
    rows = con.execute("select total from invoices where account = ?", (account,)).fetchall()
    return sum((Decimal(r[0]) for r in rows), Decimal("0"))


# ------------------------------------------------------------------ pages

def page(body, user=None, status="Ready"):
    nav = ""
    if user:
        nav = ('<td class="nav"><b>Menu</b><br><a href="/main">Main menu</a><a href="/customers">Customer enquiry</a>'
               '<a href="/invoice/new">Sales invoice entry</a><a href="/invoices">Invoice list</a>'
               '<a href="/reports">Reports</a><br><a href="/logout">Log off</a></td>')
    return HTMLResponse(f"""<html><head><title>Ledgerline Accounts</title><style>{STYLE}</style></head><body>
<div class="title">Ledgerline Accounts 4.2 - Sales Ledger<span>{html.escape(user or 'Not logged on')}</span></div>
<table class="frame"><tr>{nav}<td class="main">{body}</td></tr></table>
<div class="status">{html.escape(status)} &nbsp;|&nbsp; Company: WENSUM ACCOUNTANCY SERVICES &nbsp;|&nbsp; Period 07/2026</div>
</body></html>""")


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.on_event("startup")
def startup():
    init_db()


def current_user(request):
    tok = request.cookies.get("LLSESSION")
    entry = SESSIONS.get(tok)
    if not entry:
        return None
    user, last = entry
    if time.time() - last > IDLE_SECONDS:
        SESSIONS.pop(tok, None)
        return None
    SESSIONS[tok] = (user, time.time())
    return user


def need_login():
    return RedirectResponse("/?expired=1", status_code=302)


@app.get("/", response_class=HTMLResponse)
def login_page(expired: int = 0, bad: int = 0):
    msg = '<p class="err">Your session has expired. Please log on again.</p>' if expired else ""
    msg += '<p class="err">Invalid user ID or password.</p>' if bad else ""
    return page(f"""{msg}<fieldset style="width:300px"><legend>Log on</legend><form method="post" action="/login">
<table><tr><td>User ID</td><td><input type="text" name="u" size="16"></td></tr>
<tr><td>Password</td><td><input type="password" name="p" size="16"></td></tr>
<tr><td></td><td><input type="submit" value="Log on"></td></tr></table></form></fieldset>""", status="Not logged on")


@app.post("/login")
def login(u: str = Form(""), p: str = Form("")):
    if USERS.get(u) != p:
        return RedirectResponse("/?bad=1", status_code=302)
    tok = secrets.token_hex(16)
    SESSIONS[tok] = (u, time.time())
    r = RedirectResponse("/main", status_code=302)
    r.set_cookie("LLSESSION", tok)
    return r


@app.get("/logout")
def logout(request: Request):
    SESSIONS.pop(request.cookies.get("LLSESSION"), None)
    return RedirectResponse("/", status_code=302)


@app.get("/main")
def main(request: Request):
    user = current_user(request)
    if not user:
        return need_login()
    return page("<fieldset><legend>Main menu</legend>Select an option from the menu on the left.</fieldset>", user)


@app.get("/customers")
def customers(request: Request, q: str = ""):
    user = current_user(request)
    if not user:
        return need_login()
    rows = ""
    if q:
        con = db()
        found = con.execute("select * from customers where upper(name) like ? or upper(account) like ? order by account",
                            (f"%{q.upper()}%", f"%{q.upper()}%")).fetchall()
        for c in found:
            rows += (f"<tr><td>{c['account']}</td><td>{html.escape(c['name'])}</td><td>{html.escape(c['town'])}</td>"
                     f"<td align=right>{money(balance(con, c['account']))}</td></tr>")
        con.close()
        rows = rows or '<tr><td colspan=4>No matching records.</td></tr>'
        rows = f'<table class="grid"><tr><th>A/C</th><th>Name</th><th>Town</th><th>Balance</th></tr>{rows}</table>'
    return page(f"""<fieldset><legend>Customer enquiry</legend><form method="get" action="/customers">
Search: <input type="text" name="q" value="{html.escape(q)}" size="30"> <input type="submit" value="Find"></form></fieldset>{rows}""", user)


@app.get("/invoice/new")
def invoice_form(request: Request, err: str = ""):
    user = current_user(request)
    if not user:
        return need_login()
    lines = "".join(f'<tr><td>{i}</td><td><input type="text" name="d{i}" size="40"></td>'
                    f'<td><input type="text" name="q{i}" size="4"></td><td><input type="text" name="u{i}" size="9"></td></tr>'
                    for i in range(1, 6))
    e = f'<p class="err">{html.escape(err)}</p>' if err else ""
    today = datetime.now().strftime("%d/%m/%Y")
    return page(f"""{e}<form method="post" action="/invoice/post"><fieldset><legend>Sales invoice entry</legend>
<table><tr><td>A/C</td><td><input type="text" name="ac" size="8"></td>
<td>&nbsp; Date</td><td><input type="text" name="dt" size="10" value="{today}"></td></tr></table></fieldset>
<table class="grid"><tr><th>#</th><th>Details</th><th>Qty</th><th>Unit price</th></tr>{lines}</table>
<p>VAT at 20% is added on posting. <input type="submit" value="Post"></p></form>""", user, status="F2 = Post")


@app.post("/invoice/post")
async def invoice_post(request: Request):
    user = current_user(request)
    if not user:
        return need_login()
    f = await request.form()
    ac = str(f.get("ac", "")).strip().upper()
    dt = str(f.get("dt", "")).strip()
    con = db()
    try:
        if not con.execute("select 1 from customers where account = ?", (ac,)).fetchone():
            return RedirectResponse(f"/invoice/new?err=Account {ac or '(blank)'} not found.", status_code=302)
        try:
            datetime.strptime(dt, "%d/%m/%Y")
        except ValueError:
            return RedirectResponse("/invoice/new?err=Date must be DD/MM/YYYY.", status_code=302)
        items = []
        for i in range(1, 6):
            d, q, u = (str(f.get(k, "")).strip() for k in (f"d{i}", f"q{i}", f"u{i}"))
            if not (d or q or u):
                continue
            if not (d and q.isdigit() and int(q) > 0):
                return RedirectResponse(f"/invoice/new?err=Line {i}: details and a whole-number quantity are required.", status_code=302)
            try:
                price = Decimal(u)
            except InvalidOperation:
                return RedirectResponse(f"/invoice/new?err=Line {i}: invalid unit price.", status_code=302)
            exp = price.as_tuple().exponent
            if not price.is_finite() or price <= 0 or not isinstance(exp, int) or exp < -2:
                return RedirectResponse(f"/invoice/new?err=Line {i}: invalid unit price.", status_code=302)
            items.append((d[:60], int(q), price))
        if not items:
            return RedirectResponse("/invoice/new?err=Enter at least one line.", status_code=302)
        number = post_invoice(con, ac, dt, items)
        con.commit()
        total = con.execute("select total from invoices where number = ?", (number,)).fetchone()[0]
    finally:
        con.close()
    return page(f'<p class="ok">Invoice {number} posted to {ac}.</p><p>Invoice total {money(total)}</p>'
                f'<p><a href="/invoice/view?n={number}">View invoice</a> | <a href="/invoice/new">Enter another</a></p>', user,
                status=f"Posted {number}")


@app.get("/invoice/view")
def invoice_view(request: Request, n: str = ""):
    user = current_user(request)
    if not user:
        return need_login()
    con = db()
    inv = con.execute("select i.*, c.name from invoices i join customers c using(account) where number = ?", (n,)).fetchone()
    if not inv:
        con.close()
        return page('<p class="err">Invoice not found.</p>', user)
    lines = con.execute("select * from lines where invoice = ? order by n", (n,)).fetchall()
    con.close()
    rows = "".join(f"<tr><td>{l['n']}</td><td>{html.escape(l['description'])}</td><td align=right>{l['qty']}</td>"
                   f"<td align=right>{money(l['unit'])}</td><td align=right>{money(l['amount'])}</td></tr>" for l in lines)
    return page(f"""<fieldset><legend>Invoice {inv['number']}</legend>
<table><tr><td>A/C</td><td>{inv['account']} {html.escape(inv['name'])}</td></tr><tr><td>Date</td><td>{inv['date']}</td></tr></table></fieldset>
<table class="grid"><tr><th>#</th><th>Details</th><th>Qty</th><th>Unit</th><th>Amount</th></tr>{rows}
<tr><td colspan=4 align=right>Net</td><td align=right>{money(inv['net'])}</td></tr>
<tr><td colspan=4 align=right>VAT</td><td align=right>{money(inv['vat'])}</td></tr>
<tr><td colspan=4 align=right><b>Total</b></td><td align=right><b>{money(inv['total'])}</b></td></tr></table>""", user)


@app.get("/invoices")
def invoice_list(request: Request, ac: str = ""):
    user = current_user(request)
    if not user:
        return need_login()
    con = db()
    q = "select * from invoices" + (" where account = ?" if ac else "") + " order by number desc"
    rows = con.execute(q, (ac.upper(),) if ac else ()).fetchall()
    con.close()
    body = "".join(f'<tr><td><a href="/invoice/view?n={r["number"]}">{r["number"]}</a></td><td>{r["account"]}</td>'
                   f'<td>{r["date"]}</td><td align=right>{money(r["total"])}</td></tr>' for r in rows)
    return page(f"""<fieldset><legend>Invoice list</legend><form method="get">A/C: <input type="text" name="ac" value="{html.escape(ac)}" size="8">
<input type="submit" value="List"></form></fieldset><table class="grid"><tr><th>Number</th><th>A/C</th><th>Date</th><th>Total</th></tr>{body}</table>""", user)


@app.get("/reports")
def reports(request: Request):
    user = current_user(request)
    if not user:
        return need_login()
    return page("""<fieldset><legend>Sales day book</legend><form method="post" action="/reports/daybook">
<table><tr><td>From</td><td><input type="text" name="f" size="10"></td><td>To</td><td><input type="text" name="t" size="10"></td>
<td><input type="submit" value="Export CSV"></td></tr></table></form></fieldset>""", user)


@app.post("/reports/daybook")
def daybook(request: Request, f: str = Form(""), t: str = Form("")):
    user = current_user(request)
    if not user:
        return need_login()
    try:
        d0, d1 = datetime.strptime(f, "%d/%m/%Y"), datetime.strptime(t, "%d/%m/%Y")
    except ValueError:
        return page('<p class="err">Dates must be DD/MM/YYYY.</p>', user)
    con = db()
    rows = [r for r in con.execute("select i.*, c.name from invoices i join customers c using(account) order by number")
            if d0 <= datetime.strptime(r["date"], "%d/%m/%Y") <= d1]
    con.close()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Invoice", "Date", "A/C", "Customer", "Net", "VAT", "Total"])
    for r in rows:
        w.writerow([r["number"], r["date"], r["account"], r["name"], r["net"], r["vat"], r["total"]])
    return Response(out.getvalue(), media_type="text/csv",
                    headers={"content-disposition": f'attachment; filename="DAYBOOK_{f.replace("/", "")}_{t.replace("/", "")}.CSV"'})
