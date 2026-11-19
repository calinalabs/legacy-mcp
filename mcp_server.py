"""MCP server for Ledgerline Accounts: exposes complete accounting tasks as tools.

Run by an MCP client over stdio, e.g. in a Claude Desktop / Claude Code config:
    {"command": "uv", "args": ["run", "--directory", "<repo>", "mcp_server.py"]}

The browser automation runs on one dedicated thread (Playwright's sync API is not async-safe),
and every call is serialised, like one member of staff at one keyboard.
"""
import asyncio
import concurrent.futures as cf
import os

from mcp.server.mcpserver import MCPServer

from adapter.ledgerline import Ledgerline, LedgerlineError

_pool = cf.ThreadPoolExecutor(max_workers=1)
_app = {}


def _ledger():
    if "l" not in _app:
        _app["l"] = Ledgerline(headless=os.environ.get("HEADLESS", "1") == "1",
                               screenshots=os.environ.get("SCREENSHOTS") or None)
    return _app["l"]


async def _run(method, *args, **kwargs):
    def call():
        return getattr(_ledger(), method)(*args, **kwargs)
    try:
        return await asyncio.get_running_loop().run_in_executor(_pool, call)
    except LedgerlineError as e:
        # Business errors go back to the model as a readable result it can act on.
        return {"error": str(e)}


mcp = MCPServer(
    name="ledgerline",
    instructions=(
        "Tools for the Ledgerline Accounts sales ledger (amounts in GBP, VAT at 20% added automatically). "
        "Customers are identified by account codes like FEN001: always use find_customer to get the code first. "
        "If several customers match a name, ask the user which one instead of guessing. "
        "create_invoice posts a real invoice: confirm the customer and lines are what the user asked for."
    ),
)


@mcp.tool()
async def find_customer(name: str) -> dict:
    """Search customers by part of their name or account code. Returns each match's account code, name, town and current balance (GBP, including VAT)."""
    res = await _run("find_customer", name)
    return res if isinstance(res, dict) else {"matches": res}


@mcp.tool()
async def create_invoice(account: str, items: list[dict], date: str | None = None) -> dict:
    """Post a sales invoice. account: code from find_customer (e.g. FEN001). items: 1-5 lines, each
    {"description": str, "quantity": int, "unit_price": number in GBP excluding VAT}. date: DD/MM/YYYY, default today.
    Returns the invoice number and totals after checking the posted invoice matches the request."""
    return await _run("create_invoice", account, items, date)


@mcp.tool()
async def get_invoice(invoice_number: str) -> dict:
    """Read one invoice (e.g. INV-01043): customer, date, lines, net, VAT and total."""
    return await _run("get_invoice", invoice_number)


@mcp.tool()
async def list_invoices(account: str | None = None) -> dict:
    """List invoices, newest first, optionally for one account code."""
    res = await _run("list_invoices", account)
    return res if isinstance(res, dict) else {"invoices": res}


@mcp.tool()
async def export_sales_daybook(start_date: str, end_date: str) -> dict:
    """Run the Sales day book report for a date range (DD/MM/YYYY, inclusive): every invoice with net, VAT and total, plus the count and grand total."""
    return await _run("export_sales_daybook", start_date, end_date)


if __name__ == "__main__":
    mcp.run("stdio")
