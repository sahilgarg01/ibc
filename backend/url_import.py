"""Bounded public-page crawler. Never follows private addresses or implicit redirects."""
import ipaddress
import socket
import time
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field


class ImportRequest(BaseModel):
    url: str = Field(min_length=10, max_length=2000)
    max_pages: int | None = Field(default=None, ge=1)
    max_files: int = Field(default=25, ge=1, le=500)


def public_url(url):
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("Use a public HTTP or HTTPS URL without credentials")
    if parts.port not in {None, 80, 443}:
        raise ValueError("Only standard public HTTP/HTTPS ports are supported")
    try:
        addresses = socket.getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("The public source hostname could not be resolved") from exc
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise ValueError("Private, loopback and reserved addresses cannot be imported")
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))


def fetch(client, url, limit):
    for redirect in range(6):
        url = public_url(url)
        for attempt in range(3):
            try:
                with client.stream("GET", url) as response:
                    if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                        time.sleep(attempt + 1)
                        continue
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise ValueError("Source returned a redirect without a destination")
                        url = urljoin(url, location)
                        break
                    response.raise_for_status()
                    content = bytearray()
                    for chunk in response.iter_bytes():
                        content.extend(chunk)
                        if len(content) > limit:
                            raise ValueError("Source file exceeds the download size limit")
                    return bytes(content), url
            except httpx.TransportError:
                if attempt == 2:
                    raise ValueError("Source connection failed after three attempts") from None
                time.sleep(attempt + 1)
        else:
            raise ValueError("Source could not be downloaded")
    raise ValueError("Too many source redirects")


def crawl(request, ingest, progress):
    result = {"pages": 0, "discovered": 0, "imported": 0, "duplicates": 0, "failed": 0,
              "items": [], "errors": [], "limits_reached": False}
    queue, visited, files = [request.url], set(), set()
    def import_pdf(url, content=None):
        if url in files:
            return
        files.add(url)
        result["discovered"] += 1
        try:
            if content is None:
                content, url = fetch(client, url, 15 * 1024 * 1024)
            prefix = content.find(b"%PDF-", 0, 8192)
            if prefix > 0:
                content = content[prefix:]
            record = ingest(content, urlsplit(url).path.rsplit("/", 1)[-1] or "document.pdf", url)
            result["duplicates" if record["duplicate"] else "imported"] += 1
            result["items"].append({"url": url, "removed_prefix_bytes": max(prefix, 0), **record})
        except (ValueError, httpx.HTTPError) as exc:
            result["failed"] += 1
            result["errors"].append({"url": url, "error": str(exc)[:500]})
        except Exception as exc:
            from fastapi import HTTPException
            if not isinstance(exc, HTTPException):
                raise
            result["failed"] += 1
            result["errors"].append({"url": url, "error": str(exc.detail)[:500]})
        progress(result)

    with httpx.Client(timeout=httpx.Timeout(30, connect=10), follow_redirects=False,
                      trust_env=False, headers={"User-Agent": "IBC-Research-Importer/1.0"}) as client:
        while queue and len(files) < request.max_files and (request.max_pages is None or result["pages"] < request.max_pages):
            page = queue.pop(0)
            if page in visited:
                continue
            visited.add(page)
            try:
                content, final = fetch(client, page, 15 * 1024 * 1024)
                result["pages"] += 1
                if content.find(b"%PDF-", 0, 8192) >= 0:
                    import_pdf(final, content)
                    continue
                soup = BeautifulSoup(content, "html.parser")
                for anchor in soup.select("a[href], iframe[src], embed[src], object[data]"):
                    url = urljoin(final, anchor.get("href") or anchor.get("src") or anchor.get("data"))
                    parts = urlsplit(url)
                    if parts.scheme not in {"http", "https"}:
                        continue
                    url = urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
                    if parts.path.lower().endswith(".pdf") or anchor.get("type") == "application/pdf":
                        if len(files) < request.max_files:
                            import_pdf(url)
                            if len(files) >= request.max_files:
                                result["limits_reached"] = True
                                break
                        else:
                            result["limits_reached"] = True
                    elif parts.netloc == urlsplit(final).netloc and (
                        "next" in anchor.get("rel", []) or "pagination" in str(anchor.parent.get("class", []))
                        or (parts.path == urlsplit(final).path and "page=" in parts.query)
                    ) and url not in visited and url not in queue:
                        queue.append(url)
            except (ValueError, httpx.HTTPError) as exc:
                result["errors"].append({"url": page, "error": str(exc)[:500]})
            progress(result)
    result["limits_reached"] = result["limits_reached"] or bool(queue) or len(files) >= request.max_files
    if not result["discovered"] and not result["errors"]:
        result["errors"].append({"url": request.url, "error": "No public PDF links found. The page may require JavaScript, a form submission or login."})
    return result
