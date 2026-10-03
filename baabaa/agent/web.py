"""web_fetch and web_search for the agent. They run in the server, so they refuse private and local
addresses (no reaching the router, other LAN devices, Ollama or baabaa itself)."""

import asyncio
import html
import ipaddress
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) baabaa-agent"
MAX_BYTES = 3 * 2**20


class FetchError(Exception):
    pass


def _check_host(host: str) -> None:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise FetchError(f"cannot resolve {host}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            raise FetchError(f"{host} resolves to a private or local address; baabaa does not fetch those")


class _NoPrivateRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_host(urllib.parse.urlsplit(newurl).hostname or "")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _get(url: str, data: bytes | None = None, timeout: float = 20.0) -> tuple[bytes, str, str]:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise FetchError("only http and https URLs")
    _check_host(parts.hostname or "")
    from ..util import ssl_context
    opener = urllib.request.build_opener(_NoPrivateRedirects(), urllib.request.HTTPSHandler(context=ssl_context()))
    req = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT, "Accept-Language": "en"})
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read(MAX_BYTES)
            return body, resp.headers.get("Content-Type", ""), resp.geturl()
    except urllib.error.HTTPError as exc:
        raise FetchError(f"HTTP {exc.code} from {parts.hostname}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise FetchError(f"could not fetch {url}: {getattr(exc, 'reason', exc)}") from exc


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "template", "iframe", "head"}
    BLOCK = {"p", "div", "section", "article", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "pre",
             "blockquote", "header", "footer", "nav", "table", "ul", "ol", "dd", "dt", "figcaption"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip, self.title, self._in_title, self.href = [], 0, "", False, None

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        if tag == "title":
            self._in_title = True
        if self.skip:
            return
        if tag in ("h1", "h2", "h3"):
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag in self.BLOCK:
            self.out.append("\n")
        elif tag == "a":
            self.href = dict(attrs).get("href")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        if tag == "title":
            self._in_title = False
        if tag == "a" and self.href and not self.skip:
            if self.href.startswith("http"):
                self.out.append(f" ({self.href})")
            self.href = None
        if tag in self.BLOCK and not self.skip:
            self.out.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if not self.skip:
            self.out.append(data)

    def text(self) -> str:
        t = "".join(self.out)
        t = re.sub(r"[ \t\r\f\v]+", " ", t)
        t = re.sub(r"\n\s*\n\s*(\n\s*)+", "\n\n", t)
        return "\n".join(line.strip() for line in t.splitlines()).strip()


def html_to_text(page: str) -> tuple[str, str]:
    p = _Text()
    try:
        p.feed(page)
        p.close()
    except Exception:
        return "", re.sub(r"<[^>]+>", " ", page)
    return p.title.strip(), p.text()


async def fetch(url: str, max_chars: int = 20000) -> dict:
    body, ctype, final = await asyncio.to_thread(_get, url)
    charset = re.search(r"charset=([\w-]+)", ctype or "")
    text = body.decode(charset.group(1) if charset else "utf-8", "replace")
    title = ""
    if "html" in ctype or text.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
        title, text = html_to_text(text)
    elif not (ctype.startswith("text/") or "json" in ctype or "xml" in ctype or ctype == ""):
        return {"url": final, "title": "", "content": f"[{ctype} content, {len(body)} bytes; not shown as text]", "truncated": False}
    truncated = len(text) > max_chars
    return {"url": final, "title": title, "content": text[:max_chars], "truncated": truncated}


class _DDG(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results, self._cur, self._field = [], None, None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class", "")
        if tag == "a" and "result__a" in cls:
            self._cur = {"title": "", "url": _ddg_url(a.get("href", "")), "snippet": ""}
            self.results.append(self._cur)
            self._field = "title"
        elif tag in ("a", "div", "td") and "result__snippet" in cls and self._cur is not None:
            self._field = "snippet"

    def handle_endtag(self, tag):
        if tag in ("a", "div", "td"):
            self._field = None

    def handle_data(self, data):
        if self._cur is not None and self._field:
            self._cur[self._field] += data


def _ddg_url(href: str) -> str:
    if href.startswith("//duckduckgo.com/l/") or "uddg=" in href:
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query)
        if "uddg" in q:
            return q["uddg"][0]
    return html.unescape(href)


async def search(query: str, max_results: int = 8) -> list[dict]:
    data = urllib.parse.urlencode({"q": query}).encode()
    body, _, _ = await asyncio.to_thread(_get, "https://html.duckduckgo.com/html/", data)
    p = _DDG()
    p.feed(body.decode("utf-8", "replace"))
    out = []
    for r in p.results:
        if r["url"].startswith("http") and "duckduckgo.com/y.js" not in r["url"]:
            out.append({"title": " ".join(r["title"].split()), "url": r["url"], "snippet": " ".join(r["snippet"].split())})
        if len(out) >= max_results:
            break
    return out
