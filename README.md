# Calina Labs: an MCP adapter for a legacy app with no API

An AI agent that does real work in an old business application, here a fictional early-2000s accounting system called *Ledgerline Accounts 4.2*, which has no API, no export except CSV, unlabelled form fields and sessions that expire. The adapter operates the app's own screens and exposes complete tasks to the AI through the [Model Context Protocol](https://modelcontextprotocol.io) (MCP).

Reference build by [Calina Labs](https://calinalabs.com). Write-up with screenshots: [Connecting a legacy app with no API to an AI agent](https://calinalabs.com/blog/legacy-app-mcp-adapter/).

## How it fits together

```
Claude (or any MCP client)
   │  calls tools: find_customer, create_invoice, get_invoice, list_invoices, export_sales_daybook
   ▼
mcp_server.py   MCP server over stdio
   ▼
adapter/ledgerline.py   one method per task: navigate, fill, submit, read the result, verify, recover
   ▼  Playwright, driving the installed Chrome
legacy_app/   Ledgerline Accounts 4.2 (server-rendered HTML, no API)
```

Tools are whole tasks, not clicks. `create_invoice(account, items, date)` validates the request, fills the entry form, posts it, reads the invoice number from the confirmation page, then opens the posted invoice and checks the account, date, every line and the total before returning. Business problems (unknown account, more than five lines, a zero price, an impossible date) come back as readable errors the AI can act on, and nothing is posted.

## Results

13 tasks, each from a fresh database, checked in the database rather than from the agent's reply. Model: Claude Opus 5.5.

| | |
|---|---|
| Tasks passed | 13 / 13 |
| Includes | ambiguous customer names, unknown customers, a 7-line invoice in a 5-line system, invalid dates and prices, reports, balances |
| Tool calls | 22 in total (under 2 per task) |
| Average time per task | about 6 s |

Two notes on honesty: one task's expected total was wrong in our first draft (£882, the right figure is £852) and its wording asked for "one invoice" the system cannot hold; the agent refused to post and explained, which is the right behaviour, so the task was changed to check for that and a second task was added that allows splitting. One check missed the phrase "can't find" and was extended. The published results are a fresh run of all 13 tasks with the final checks, in `results/eval.json`.

## Run it

```bash
uv sync
uv run uvicorn legacy_app.app:app --port 8050        # the legacy app: http://127.0.0.1:8050 (accounts / ledger2004)
uv run agent.py "Invoice Fenwick Builders for 2 site visits at £120 each"
uv run eval.py --shots                               # all tasks, with screenshots of every page touched
uv run pytest                                        # adapter tests, including session-expiry recovery (no API key needed)
```

Keys: `ANTHROPIC_API_KEY` in the environment or `~/.config/calinalabs/keys.env`. Playwright uses the installed Chrome (`BROWSER_CHANNEL=chrome`), so no browser download is needed.

Use it from Claude Desktop or Claude Code by adding the server to the MCP config:

```json
{"mcpServers": {"ledgerline": {"command": "uv", "args": ["run", "--directory", "/path/to/repo", "mcp_server.py"]}}}
```

## Adapting it to a real legacy app

| What the app offers | How the adapter talks to it |
|---|---|
| An API or SDK | Call it directly and wrap the calls as MCP tools |
| A command-line interface | Run commands and parse their output |
| File import / export | Read exports, write import files |
| A web interface only | Drive the pages (this repo, Playwright) |
| A desktop interface only | Desktop automation (UI Automation on Windows) |

The limits are the app's: MCP cannot make an unreadable screen readable or skip a login it requires, and screen automation breaks when screens change, so every task verifies its result and the adapter has tests that run against the real screens.

---

Built by [Calina Labs](https://calinalabs.com), a London AI engineering studio. [Get in touch](https://calinalabs.com/contact/).
