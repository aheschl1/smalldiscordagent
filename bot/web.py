"""`curl`: HTTP requests to the public internet, for write users only.

GET/HEAD run freely; anything else can have side effects, so it pauses for the user's Confirm click.
Private, loopback and link-local addresses are refused (the bot's host sits on a private network, and cloud
metadata lives on link-local). Each hop's host is resolved once, checked, and the connection pinned to that
IP (TLS still verifies the real hostname), so DNS rebinding and redirects can't reach an internal address.
Bodies too big to return are saved as an artifact for artifact_read/artifact_grep.
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import re
import socket
from urllib.parse import urljoin, urlsplit

import httpx

from . import artifacts
from .apis import gate

MAX_BYTES = 5_000_000
MAX_REDIRECTS = 5
TIMEOUT_S = 20
TEXTUAL = re.compile(r"^text/|json|xml|javascript|x-www-form-urlencoded|yaml|csv")
SHOW_HEADERS = ("content-type", "content-length", "location", "last-modified", "etag", "retry-after")


async def _public_ip(host: str) -> str:
    """Resolve host; refuse if any address isn't globally routable. Returns the address to connect to."""
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ValueError(f"can't resolve {host}: {e}") from None
    ips = [ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos]
    if not ips or any(not ip.is_global or ip.is_multicast for ip in ips):
        raise ValueError(f"{host} resolves to a private/internal address; only public hosts are allowed")
    return str(min(ips, key=lambda ip: ip.version))  # prefer IPv4; not every host has v6 connectivity


def _to_text(body: str) -> str:
    body = re.sub(r"(?is)<(script|style|noscript|svg|head)\b.*?</\1>|<!--.*?-->", "", body)
    body = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h\d|pre|section|article|table)>", "\n", body)
    body = html.unescape(re.sub(r"<[^>]+>", "", body))
    return re.sub(r"\n\s*\n+", "\n\n", re.sub(r"[ \t\r\f\v]+", " ", body)).strip()


async def _fetch(method: str, url: str, headers: dict, body: str | None) -> tuple[httpx.Response, bytes, str, bool]:
    async with httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=False, trust_env=False) as client:
        for _ in range(MAX_REDIRECTS + 1):
            u = urlsplit(url)
            if u.scheme not in ("http", "https") or not u.hostname:
                raise ValueError("url must be http(s)://host/...")
            ip = await _public_ip(u.hostname)
            pinned = u._replace(netloc=(f"[{ip}]" if ":" in ip else ip) + (f":{u.port}" if u.port else "")).geturl()
            req = client.build_request(method, pinned, headers={**headers, "Host": u.netloc.rsplit("@", 1)[-1]},
                                       content=body, extensions={"sni_hostname": u.hostname})
            r = await client.send(req, stream=True)
            try:
                if r.is_redirect and "location" in r.headers:
                    url = urljoin(url, r.headers["location"])
                    if r.status_code in (301, 302, 303) and method != "HEAD":
                        method, body = "GET", None
                    continue
                data, cut = b"", False
                async for part in r.aiter_bytes():
                    data += part
                    if len(data) > MAX_BYTES:
                        data, cut = data[:MAX_BYTES], True
                        break
                return r, data, url, cut
            finally:
                await r.aclose()
    raise ValueError(f"more than {MAX_REDIRECTS} redirects")


SCHEMA = {
    "type": "function", "name": "curl", "strict": False,
    "description": "HTTP request to a public URL (docs, APIs, status pages, raw files). HTML is returned as text "
                   "unless raw=true. GET/HEAD run directly; other methods ask the user to confirm. Large bodies are "
                   "saved as an artifact to grep/read.",
    "parameters": {"type": "object", "properties": {
        "url": {"type": "string"},
        "method": {"type": "string", "enum": ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]},
        "headers": {"type": "object", "description": "extra request headers"},
        "body": {"description": "request body: a string, or an object sent as JSON"},
        "raw": {"type": "boolean", "description": "return HTML source instead of extracted text"},
        "why": {"type": "string", "description": "one line, shown on confirm"},
    }, "required": ["url"]},
}


async def _curl(a: dict, c) -> str:
    method = (a.get("method") or "GET").upper()
    headers = {str(k): str(v) for k, v in (a.get("headers") or {}).items()}
    headers.setdefault("User-Agent", "ucbcagent/0.1 (Discord code assistant)")
    body = a.get("body")
    if body is not None and not isinstance(body, str):
        body = json.dumps(body)
        headers.setdefault("Content-Type", "application/json")
    if method not in ("GET", "HEAD"):
        summary = (f"HTTP `{method} {a['url']}`" + (f"\n```\n{body[:800]}\n```" if body else "")
                   + (f"\n{a['why']}" if a.get("why") else ""))
        if err := await gate("danger", summary, c):
            return err
    try:
        r, data, final, cut = await _fetch(method, a["url"], headers, body)
    except httpx.HTTPError as e:
        return f"error: {type(e).__name__}: {e}"

    head = f"{r.status_code} {r.reason_phrase}" + (f" (after redirects: {final})" if final != a["url"] else "")
    head += "".join(f"\n{k}: {r.headers[k]}" for k in SHOW_HEADERS if k in r.headers)
    if method == "HEAD" or not data:
        return head
    ctype = r.headers.get("content-type", "").lower()
    if not TEXTUAL.search(ctype):
        return f"{head}\n[{len(data)} bytes of non-text content]"
    text = data.decode(r.encoding or "utf-8", errors="replace")
    if "html" in ctype and not a.get("raw"):
        text = _to_text(text)
    note = f"\n[body truncated at {MAX_BYTES} bytes]" if cut else ""
    if len(head) + len(text) + len(note) + 2 <= c.max_out:
        return f"{head}\n\n{text}{note}"
    aid, meta = artifacts.save(text.encode(), urlsplit(final).path.rsplit("/", 1)[-1] or "response.txt",
                               c.requester, final)
    return (f"{head}\n[body is {meta['lines']} lines, {meta['bytes']} bytes: saved as artifact id={aid}; use "
            f"artifact_grep/artifact_read]{note}\n\n{text[:2000]}…")


def tools() -> list:
    return [(SCHEMA, _curl)]
