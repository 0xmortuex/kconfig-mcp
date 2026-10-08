"""MCP stdio smoke test: the real server subprocess, its tool list, and that
instructive errors survive the trip (MCPServer hides non-ToolError text)."""

import os
import sys

import pytest

pytest.importorskip("mcp")

from mcp import ClientSession  # noqa: E402
from mcp.client.stdio import StdioServerParameters, stdio_client  # noqa: E402

TREE = os.path.join(os.path.dirname(__file__), "fixtures", "tree", "Kconfig")

EXPECTED_TOOLS = {
    "kconfig_load",
    "kconfig_unload",
    "kconfig_sessions",
    "kconfig_symbol",
    "kconfig_why_not",
    "kconfig_what_selects",
    "kconfig_search",
    "kconfig_set",
    "kconfig_diff",
    "kconfig_save",
}


@pytest.mark.anyio
async def test_tools_and_errors_over_stdio():
    params = StdioServerParameters(command=sys.executable, args=["-m", "kconfig_mcp"])
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        assert init.server_info.name == "kconfig"
        names = {t.name for t in (await session.list_tools()).tools}
        assert names == EXPECTED_TOOLS, (
            f"missing: {EXPECTED_TOOLS - names}, unexpected: {names - EXPECTED_TOOLS}")

        # Missing env var: the agent must see which variable to pass.
        res = await session.call_tool("kconfig_load", {"name": "t", "kconfig_path": TREE})
        assert res.is_error and "undefined variable(s): SRCARCH" in res.content[0].text

        res = await session.call_tool(
            "kconfig_load", {"name": "t", "kconfig_path": TREE, "env": {"SRCARCH": "x86"}})
        assert not res.is_error

        res = await session.call_tool("kconfig_why_not", {"name": "t", "symbol": "WIFI_DRIVER"})
        assert "Verified fix" in res.content[0].text

        res = await session.call_tool("kconfig_symbol", {"name": "t", "symbol": "WIFI_DRVER"})
        assert res.is_error and "Did you mean: WIFI_DRIVER" in res.content[0].text


@pytest.fixture
def anyio_backend():
    return "asyncio"
