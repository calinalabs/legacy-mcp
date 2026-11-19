"""A small agent that uses the Ledgerline MCP server: Claude decides which tools to call; the adapter operates the app.

    uv run agent.py "Invoice Fenwick Builders for 3 site visits at £120 and one report at £450"

Keys: ANTHROPIC_API_KEY (or ~/.config/calinalabs/keys.env).
"""
import asyncio
import json
import os
import pathlib
import sys

import anthropic
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = pathlib.Path(__file__).resolve().parent
MODEL = os.environ.get("AGENT_MODEL", "claude-opus-5-5")
SYSTEM = (
    "You are an accounts assistant at Wensum Accountancy Services. You work in the Ledgerline sales ledger "
    "only through the tools provided. Today is {today}. Use UK date format DD/MM/YYYY. "
    "If a request is ambiguous (for example several customers match a name), do not post anything: "
    "say what is ambiguous and stop. After acting, reply briefly with what you did, including invoice numbers and totals."
)


def load_keys():
    f = pathlib.Path.home() / ".config" / "calinalabs" / "keys.env"
    if f.exists():
        for line in f.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def tool_text(result):
    """Turn an MCP CallToolResult into the text Claude reads."""
    if getattr(result, "structured_content", None):
        return json.dumps(result.structured_content)
    return "\n".join(getattr(c, "text", "") for c in result.content)


async def run(task, today="18/11/2026", log=print, server_env=None):
    """Run one task. Returns {"answer": str, "calls": [(tool, input, output)], "usage": {...}}."""
    load_keys()
    client = anthropic.AsyncAnthropic()
    params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "mcp_server.py")], cwd=str(ROOT),
                                   env={**os.environ, **(server_env or {})})
    calls, usage = [], {"input": 0, "output": 0}
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            tools = [{"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
                     for t in listed.tools]
            messages = [{"role": "user", "content": task}]
            for _ in range(15):  # hard stop on runaway loops
                response = await client.messages.create(
                    model=MODEL, max_tokens=16000, system=SYSTEM.format(today=today),
                    tools=tools, messages=messages)
                usage["input"] += response.usage.input_tokens
                usage["output"] += response.usage.output_tokens
                if response.stop_reason != "tool_use":
                    answer = "".join(b.text for b in response.content if b.type == "text").strip()
                    return {"answer": answer, "calls": calls, "usage": usage, "stop_reason": response.stop_reason}
                messages.append({"role": "assistant", "content": response.content})
                results = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    log(f"→ {block.name}({json.dumps(block.input)})")
                    res = await session.call_tool(block.name, block.input)
                    text = tool_text(res)
                    log(f"← {text[:300]}")
                    calls.append((block.name, block.input, text))
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": text,
                                    "is_error": bool(getattr(res, "is_error", False)) or '"error"' in text[:20]})
                messages.append({"role": "user", "content": results})
    return {"answer": "Stopped: too many steps.", "calls": calls, "usage": usage, "stop_reason": "max_steps"}


if __name__ == "__main__":
    out = asyncio.run(run(" ".join(sys.argv[1:]) or "Which customers are in Cambridge? Search for Kestrel."))
    print("\n" + out["answer"])
