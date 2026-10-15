import asyncio
import ipaddress
import socket
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("Only public HTTP and HTTPS URLs are allowed")
    host = parts.hostname.lower().rstrip(".")
    display_host = f"[{host}]" if ":" in host else host
    path = parts.path.rstrip("/") or "/"
    tracking = {"fbclid", "gclid", "mc_cid", "mc_eid"}
    query = urlencode([
        (key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_") and key.casefold() not in tracking
    ])
    return urlunsplit((parts.scheme.lower(), display_host + (f":{parts.port}" if parts.port else ""), path, query, ""))


async def is_public_url(url: str) -> bool:
    try:
        parts = urlsplit(normalize_url(url))
        host = parts.hostname or ""
        if parts.port is not None and parts.port not in {80, 443}:
            return False
        if host in {"localhost", "metadata.google.internal"} or host.endswith((".localhost", ".local", ".internal")):
            return False
        try:
            ip = ipaddress.ip_address(host)
            return ip.is_global
        except ValueError:
            pass
        infos = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM), timeout=3)
        return bool(infos) and all(ipaddress.ip_address(info[4][0]).is_global for info in infos)
    except (ValueError, OSError, asyncio.TimeoutError):
        return False
