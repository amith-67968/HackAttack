from typing import Any

import httpx


class TavilyError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, retry_after_seconds: float | str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


class TavilySearch:
    def __init__(self, api_key: str, timeout_seconds: float = 30.0, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    async def search(self, query: str, *, domain: str | None = None, max_results: int = 5) -> list[dict[str, Any]]:
        body: dict[str, Any] = {
            "api_key": self.api_key,
            "query": query,
            "search_depth": "basic",
            "max_results": max_results,
            "include_answer": False,
        }
        if domain:
            body["include_domains"] = [domain]
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self.transport) as client:
                response = await client.post("https://api.tavily.com/search", json=body)
                response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise TavilyError("Tavily returned an invalid response object")
            results = payload.get("results", [])
            if not isinstance(results, list):
                raise TavilyError("Tavily returned an invalid results collection")
            return [item for item in results if isinstance(item, dict) and item.get("url")]
        except (httpx.HTTPError, ValueError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            category = "rate limited" if status == 429 else "request failed"
            headers = getattr(getattr(exc, "response", None), "headers", {})
            retry_after = headers.get("Retry-After")
            raise TavilyError(f"Tavily search {category}: {status or exc.__class__.__name__}", status, retry_after) from exc
