"""Read bounded SEC filing excerpts as untrusted research material, never instructions."""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import CancelledError
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

from .sources import PublicResearchClient

MAX_BYTES = 2_000_000
MAX_TEXT = 20_000
CACHE_TTL = timedelta(hours=24)
_PATH = re.compile(r"/Archives/edgar/data/[0-9]{1,10}/[0-9]{18}/[A-Za-z0-9][A-Za-z0-9_.-]*\.(?:htm|html|txt)$", re.I)
_FORMS = {"10-Q", "10-Q/A", "8-K", "8-K/A", "6-K", "6-K/A", "10-K", "20-F"}
_CACHE_LOCK = threading.Lock()


def _cancel(callback):
    if callback and callback():
        raise CancelledError()


def _sec_url(url: str, directory: str | None = None) -> str:
    """Reject encoded separators, query-based viewers, credentials and traversal."""
    if not isinstance(url, str) or url != url.strip() or re.search(r"[\x00-\x20\x7f\\]", url):
        raise ValueError("SEC_DOCUMENT_URL_REJECTED")
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != "www.sec.gov" or parsed.username
            or parsed.password or parsed.port not in (None, 443) or parsed.query
            or "%" in parsed.path or not _PATH.fullmatch(parsed.path)
            or any(part in {".", ".."} for part in parsed.path.split("/"))):
        raise ValueError("SEC_DOCUMENT_URL_REJECTED")
    canonical = "https://www.sec.gov" + parsed.path
    if directory and canonical.rsplit("/", 1)[0] != directory:
        raise ValueError("SEC_ATTACHMENT_OUTSIDE_ACCESSION")
    return canonical


class _SECRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, directory: str):
        super().__init__()
        self.directory = directory

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        allowed = _sec_url(newurl, self.directory)
        return super().redirect_request(req, fp, code, msg, headers, allowed)


class _Text(HTMLParser):
    DROP = {"script", "style", "nav", "header", "footer", "head", "iframe", "object", "template", "noscript", "svg", "form", "button", "ix:hidden", "ix:header", "xbrli:context", "xbrli:unit"}
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
    BLOCK = {"p", "div", "tr", "li", "h1", "h2", "h3", "h4", "table", "section", "br"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.parts, self.links, self.anchors = [], [], [], []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        style = str(attributes.get("style", "")).replace(" ", "").lower()
        hidden = (bool(self.stack and self.stack[-1][1]) or tag in self.DROP
                  or "hidden" in attributes or str(attributes.get("aria-hidden", "")).lower() == "true"
                  or "display:none" in style or "visibility:hidden" in style
                  or attributes.get("role") in {"navigation", "banner", "contentinfo"})
        if not hidden and tag in self.BLOCK:
            self.parts.append("\n")
        if tag == "a" and not hidden:
            self.anchors.append({"href": attributes.get("href", ""), "text": [],
                                 "exhibit": "-sec-extract:exhibit" in style})
        if tag not in self.VOID:
            self.stack.append((tag, hidden))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == "a" and self.anchors:
            anchor = self.anchors.pop()
            anchor["text"] = " ".join(anchor["text"])
            self.links.append(anchor)
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                self.stack = self.stack[:i]
                break
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, text):
        if not self.stack or not self.stack[-1][1]:
            self.parts.append(text)
            if self.anchors:
                self.anchors[-1]["text"].append(text)

    def text(self) -> str:
        lines = [re.sub(r"\s+", " ", line).strip() for line in "".join(self.parts).splitlines()]
        return "\n".join(line for line in lines if line)


def _extract(raw: bytes) -> tuple[str, list]:
    parser = _Text()
    # SEC filings are generally UTF-8; legacy bytes are replacement-decoded, not executed.
    parser.feed(raw.decode("utf-8", errors="replace"))
    parser.close()
    return parser.text(), parser.links


def _excerpt(text: str) -> tuple[str, int]:
    # Prefer the substantive later MD&A heading over an early table-of-contents hit.
    matches = list(re.finditer(r"^(?:item\s*2[.\s]*)?management.{0,12}discussion and analysis[^\n]*", text, re.I | re.M))
    substantial = [m for m in matches if len(text) - m.start() > 1000
                   and not re.search(r"^\s*\d+\s*\nItem\s*[34]", text[m.end():m.end() + 200], re.I)]
    start = substantial[0].start() if substantial else 0
    return text[start:start + MAX_TEXT], start


def _read_document(url: str, publication_date: str, cache_dir: Path | None, cancelled=None):
    url = _sec_url(url)
    _cancel(cancelled)
    key = hashlib.sha256(url.encode()).hexdigest()
    now = datetime.now(UTC)
    source = {"id": "sec-doc-" + key[:12], "source_id": "sec-doc-" + key[:12],
              "provider": "SEC", "url": url, "retrieved_at": None, "attempted_at": now.isoformat(),
              "published_at": publication_date, "publication_date": publication_date,
              "sha256": None, "bytes": 0, "coverage": "index_only", "status": "missing",
              "stale": False, "untrusted": True, "error_code": None}
    raw_path = Path(cache_dir) / (key + ".html") if cache_dir else None
    meta_path = Path(cache_dir) / (key + ".json") if cache_dir else None
    try:
        # Share the public research client's limiter across threads and modules.
        with PublicResearchClient._rate_lock:
            delay = max(0, PublicResearchClient._next_request - time.monotonic())
            while delay > 0:
                _cancel(cancelled)
                time.sleep(min(.1, delay))
                delay = max(0, PublicResearchClient._next_request - time.monotonic())
            PublicResearchClient._next_request = time.monotonic() + .15
        _cancel(cancelled)
        opener = urllib.request.build_opener(_SECRedirect(url.rsplit("/", 1)[0]))
        request = urllib.request.Request(url, headers={"User-Agent": "VerdictQuant desktop public research", "Accept": "text/html,text/plain"})
        with opener.open(request, timeout=12) as response:
            _sec_url(response.geturl(), url.rsplit("/", 1)[0])
            length = response.headers.get("Content-Length")
            if length and int(length) > MAX_BYTES:
                raise ValueError("SEC_DOCUMENT_TOO_LARGE")
            # Read no more than the configured limit. A full-size read is rejected
            # conservatively rather than issuing another byte read past the limit.
            raw = response.read(MAX_BYTES)
        _cancel(cancelled)
        if len(raw) >= MAX_BYTES:
            raise ValueError("SEC_DOCUMENT_TOO_LARGE")
        if not raw.strip():
            raise ValueError("SEC_DOCUMENT_EMPTY")
        source.update(status="ok", coverage="excerpt", retrieved_at=datetime.now(UTC).isoformat(),
                      sha256=hashlib.sha256(raw).hexdigest(), bytes=len(raw))
        if raw_path:
            try:
                raw_path.parent.mkdir(parents=True, exist_ok=True)
                with _CACHE_LOCK:
                    raw_temp = raw_path.with_name(raw_path.name + "." + str(threading.get_ident()) + ".tmp")
                    meta_temp = meta_path.with_name(meta_path.name + "." + str(threading.get_ident()) + ".tmp")
                    raw_temp.write_bytes(raw)
                    meta_temp.write_text(json.dumps(source), encoding="utf-8")
                    raw_temp.replace(raw_path)
                    meta_temp.replace(meta_path)
            except OSError:
                source["cache_warning"] = "DOCUMENT_CACHE_WRITE_FAILED"
        return raw, source
    except CancelledError:
        raise
    except urllib.error.HTTPError as exc:
        source["error_code"] = "HTTP_" + str(exc.code)
    except (urllib.error.URLError, TimeoutError, OSError):
        source["error_code"] = "SEC_DOCUMENT_NETWORK_UNAVAILABLE"
    except (ValueError, TypeError):
        source["error_code"] = "SEC_DOCUMENT_REJECTED_OR_TOO_LARGE"
    if raw_path:
        try:
            with _CACHE_LOCK:
                if raw_path.stat().st_size >= MAX_BYTES or meta_path.stat().st_size > 20_000:
                    raise ValueError("CACHE_TOO_LARGE")
                raw = raw_path.read_bytes()
                previous = json.loads(meta_path.read_text(encoding="utf-8"))
            retrieved = datetime.fromisoformat(previous["retrieved_at"])
            if (retrieved.tzinfo is None or retrieved > now or previous["url"] != url
                    or previous["publication_date"] != publication_date
                    or hashlib.sha256(raw).hexdigest() != previous["sha256"]):
                raise ValueError("DOCUMENT_CACHE_INVALID")
            source.update(status="stale", stale=True, coverage="excerpt", retrieved_at=previous["retrieved_at"],
                          sha256=previous["sha256"], bytes=len(raw), cache_expired=now - retrieved > CACHE_TTL)
            return raw, source
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return None, source


def collect_documents(filings: list, as_of: datetime, cache_dir: Path | None = None, cancelled=None) -> dict:
    """At most two recent filing excerpts and one same-accession exhibit per cover.

    Text is untrusted material for a candidate summary. Excerpts and index-only
    failures do not constitute a complete filing or risk-factor review.
    """
    if not isinstance(as_of, datetime) or as_of.tzinfo is None:
        raise ValueError("DOCUMENT_AS_OF_TIMEZONE_REQUIRED")
    at = as_of.astimezone(UTC)
    result = {"documents": [], "sources": [], "errors": [],
              "coverage": "excerpts_only_not_complete_risk_review", "untrusted": True}
    candidates = []
    for filing in filings:
        _cancel(cancelled)
        try:
            day = datetime.fromisoformat(filing["filing_date"]).date()
            if filing.get("form") not in _FORMS or not at.date() - timedelta(days=90) <= day <= at.date():
                continue
            if day == at.date():
                accepted = filing.get("accepted_at") or filing.get("published_at")
                if not accepted:
                    result["errors"].append("SAME_DAY_FILING_ACCEPTANCE_TIME_UNAVAILABLE")
                    continue
                stamp = datetime.fromisoformat(accepted.replace("Z", "+00:00"))
                if stamp.tzinfo is None or stamp.astimezone(UTC) > at:
                    continue
            url = _sec_url(filing["url"])
            candidates.append({**filing, "url": url})
        except (KeyError, TypeError, ValueError):
            result["errors"].append("FILING_URL_OR_DATE_REJECTED")
    candidates = sorted({r["url"]: r for r in candidates}.values(), key=lambda r: r["filing_date"], reverse=True)
    quarterly = next((r for r in candidates if r["form"].startswith("10-Q")), None)
    selected = []
    if quarterly:
        selected.append(quarterly)
        companion = next((r for r in candidates if r["form"].startswith("8-K") and r["filing_date"] == quarterly["filing_date"]), None)
        if companion:
            selected.append(companion)
    for filing in candidates:
        if len(selected) >= 2:
            break
        if filing not in selected:
            selected.append(filing)
    for filing in selected:
        _cancel(cancelled)
        raw, source = _read_document(filing["url"], filing["filing_date"], cache_dir, cancelled)
        result["sources"].append(source)
        if source["status"] != "ok":
            result["errors"].append(source["id"] + ":" + str(source["error_code"]))
        if raw is None:
            continue
        text, links = _extract(raw)
        refs = [source["id"]]
        chosen_source = source
        # A quarterly press-release exhibit gives more evidence than an 8-K cover.
        if filing["form"].startswith(("8-K", "6-K")):
            exhibits = []
            for link in links:
                label = link["text"] + " " + link["href"]
                if not (re.search(r"ex(?:hibit)?[\s_.-]*99|earnings|financial results|press release|cfo.{0,4}commentary", label, re.I)
                        or link["exhibit"] and re.search(r"99\.[12]|q[1-4].*(?:pr|cfo)", label, re.I)):
                    continue
                try:
                    exhibit = _sec_url(urllib.parse.urljoin(filing["url"], link["href"]), filing["url"].rsplit("/", 1)[0])
                    if exhibit != filing["url"] and exhibit not in exhibits:
                        exhibits.append(exhibit)
                except (ValueError, TypeError):
                    result["errors"].append("SEC_ATTACHMENT_LINK_REJECTED")
            if exhibits:
                attachment, attachment_source = _read_document(exhibits[0], filing["filing_date"], cache_dir, cancelled)
                result["sources"].append(attachment_source)
                if attachment_source["status"] != "ok":
                    result["errors"].append(attachment_source["id"] + ":" + str(attachment_source["error_code"]))
                if attachment:
                    text, _ = _extract(attachment)
                    refs.append(attachment_source["id"])
                    chosen_source = attachment_source
        excerpt, offset = _excerpt(text)
        if not excerpt:
            result["errors"].append(chosen_source["id"] + ":NO_VISIBLE_DOCUMENT_TEXT")
            continue
        result["documents"].append({"title": str(filing.get("title", filing["form"]))[:300],
            "text": excerpt, "url": chosen_source["url"], "source_id": chosen_source["id"], "source_refs": refs,
            "publication_date": filing["filing_date"], "published_at": filing["filing_date"],
            "retrieved_at": chosen_source["retrieved_at"], "sha256": chosen_source["sha256"],
            "coverage": "excerpt", "excerpt_start_char": offset, "truncated": offset > 0 or len(text) > MAX_TEXT,
            "stale": chosen_source["stale"], "untrusted": True, "complete_risk_review": False})
    if not result["documents"]:
        result["errors"].append("FILINGS_INDEX_ONLY_NO_DOCUMENT_TEXT")
    return result
