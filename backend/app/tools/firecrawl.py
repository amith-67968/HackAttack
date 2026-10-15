from typing import Any

import httpx


class FirecrawlError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class FirecrawlScraper:
    def __init__(self, api_key: str, timeout_seconds: float = 30.0, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    async def scrape(self, url: str) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds, transport=self.transport) as client:
                response = await client.post(
                    "https://api.firecrawl.dev/v1/scrape",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={"url": url, "formats": ["markdown"]},
                )
                response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise FirecrawlError("Firecrawl returned an invalid response object")
            data = payload.get("data") or payload
            if not isinstance(data, dict):
                raise FirecrawlError("Firecrawl returned invalid page metadata")
            markdown = data.get("markdown") or data.get("content") or ""
            if not isinstance(markdown, str) or not markdown.strip():
                raise FirecrawlError("Firecrawl returned no readable page content")
            metadata = data.get("metadata") or {}
            return {"markdown": markdown, "metadata": metadata}
        except FirecrawlError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None)
            category = "rate limited" if status == 429 else "request failed"
            raise FirecrawlError(f"Firecrawl {category}: {status or exc.__class__.__name__}", status) from exc
