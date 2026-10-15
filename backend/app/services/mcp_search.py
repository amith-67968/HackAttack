"""MCP protocol boundary for the existing Tavily search adapter."""

from typing import Any

from mcp import Client
from mcp.server import MCPServer


class MCPToolError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, retry_after_seconds: float | str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


def create_search_server(search_adapter: Any) -> MCPServer:
    server = MCPServer(name="ProofLoop research tools", version="1.0.0")

    @server.tool(name="tavily_search", description="Search the web for public pages relevant to a focused research query.")
    async def tavily_search(query: str, domain: str | None = None, max_results: int = 5) -> dict[str, Any]:
        """Return candidate webpage metadata from Tavily."""
        if not query.strip() or len(query) > 2000 or not 1 <= max_results <= 8:
            raise ValueError("Invalid search tool input")
        try:
            return {"results": await search_adapter.search(query, domain=domain, max_results=max_results)}
        except Exception as exc:
            # SDK errors must not echo provider response bodies or credentials.
            return {"mcp_failure": True, "safe_error": f"Search provider failed ({exc.__class__.__name__})",
                    "provider_status": getattr(exc, "status_code", None),
                    "retry_after_seconds": getattr(exc, "retry_after_seconds", None)}
    return server


async def discover_and_search(search_adapter: Any, query: str, *, domain: str | None, max_results: int) -> list[dict[str, Any]]:
    server = create_search_server(search_adapter)
    async with Client(server, read_timeout_seconds=60) as client:
        available = await client.list_tools()
        tools = {tool.name: tool for tool in available.tools}
        tool = tools.get("tavily_search")
        if tool is None:
            raise MCPToolError("Required MCP search tool was not discovered")
        schema = tool.input_schema
        if not isinstance(schema, dict) or not isinstance(schema.get("properties"), dict):
            raise MCPToolError("MCP search tool has an invalid input schema")
        result = await client.call_tool("tavily_search", {"query": query, "domain": domain, "max_results": max_results}, read_timeout_seconds=60)
        if result.is_error:
            text = "MCP search tool failed"
            for item in result.content or []:
                if getattr(item, "text", None) and "Search provider failed (" in item.text:
                    text += ": " + item.text.split("Search provider failed (")[-1].split(")", 1)[0]
            raise MCPToolError(text)
        data = result.structured_content
        if not isinstance(data, dict):
            raise MCPToolError("MCP search tool returned an invalid result")
        if data.get("mcp_failure"):
            raise MCPToolError(str(data.get("safe_error") or "MCP search provider failed"),
                               data.get("provider_status"), data.get("retry_after_seconds"))
        results = data.get("results")
        if not isinstance(results, list):
            raise MCPToolError("MCP search tool returned an invalid results collection")
        return [row for row in results if isinstance(row, dict)]
