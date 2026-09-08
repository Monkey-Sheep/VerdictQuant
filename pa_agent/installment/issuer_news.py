"""Bounded official NVIDIA/Coinbase release excerpts, used only on explicit refresh."""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import CancelledError
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

from .documents import MAX_BYTES, _cancel, _excerpt, _extract
from .sources import PROJECT_CONTACT, PublicResearchClient

NVIDIA_RSS = "https://nvidianews.nvidia.com/rss.xml"
COINBASE_FEED = "https://investor.coinbase.com/feed/PressRelease.svc/GetPressReleaseList"
HOSTS = {"NVDA": "nvidianews.nvidia.com", "COIN": "investor.coinbase.com"}
_CACHE_LOCK = threading.Lock()


def _coin_feed(year: int) -> str:
    return COINBASE_FEED + "?" + urllib.parse.urlencode({
        "LanguageId": 1, "pageSize": 8, "pageNumber": 0, "tagList": "", "includeTags": "true",
        "year": year, "excludeSelection": 1, "bodyType": 2, "pressReleaseDateFilter": 1,
        "categoryId": "00000000-0000-0000-0000-000000000000",
    })


def _issuer_url(url: str, symbol: str) -> str:
    if not isinstance(url, str) or url != url.strip() or re.search(r"[\x00-\x20\x7f\\]", url):
        raise ValueError("ISSUER_URL_REJECTED")
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != HOSTS.get(symbol) or parsed.username or parsed.password
            or parsed.port not in (None, 443) or "%" in parsed.path or ".." in parsed.path):
        raise ValueError("ISSUER_URL_REJECTED")
    if symbol == "NVDA":
        valid = parsed.path == "/rss.xml" or bool(re.fullmatch(r"/news/[A-Za-z0-9-]+/?", parsed.path))
        if not valid or parsed.query:
            raise ValueError("ISSUER_PATH_REJECTED")
    else:
        if parsed.path == "/feed/PressRelease.svc/GetPressReleaseList":
            query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            try:
                year = int(query["year"][0])
            except (KeyError, ValueError, TypeError, IndexError):
                raise ValueError("ISSUER_FEED_QUERY_REJECTED") from None
            expected = urllib.parse.parse_qs(urllib.parse.urlsplit(_coin_feed(year)).query, keep_blank_values=True)
            if query != expected or not 2000 <= year <= 2100:
                raise ValueError("ISSUER_FEED_QUERY_REJECTED")
        elif not re.fullmatch(r"/news/news-details/[0-9]{4}/[A-Za-z0-9_-]+/default\.aspx", parsed.path) or parsed.query:
            raise ValueError("ISSUER_PATH_REJECTED")
    return urllib.parse.urlunsplit(("https", HOSTS[symbol], parsed.path, parsed.query, ""))


class _IssuerRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, symbol):
        super().__init__()
        self.symbol = symbol

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, _issuer_url(newurl, self.symbol))


def _request(url: str, symbol: str, coverage: str, cache_dir: Path | None, cancelled=None):
    url = _issuer_url(url, symbol)
    _cancel(cancelled)
    digest = hashlib.sha256(url.encode()).hexdigest()
    now = datetime.now(UTC)
    source = {"id": "issuer-" + symbol.lower() + "-" + digest[:12], "provider": "NVIDIA Newsroom" if symbol == "NVDA" else "Coinbase Investor Relations",
              "url": url, "retrieved_at": None, "fetched_at": None, "attempted_at": now.isoformat(),
              "sha256": None, "bytes": 0, "status": "missing", "coverage": "index_only",
              "stale": False, "untrusted": True, "error_code": None}
    raw_path = Path(cache_dir) / (digest + ".bin") if cache_dir else None
    meta_path = Path(cache_dir) / (digest + ".json") if cache_dir else None
    try:
        with PublicResearchClient._rate_lock:
            while time.monotonic() < PublicResearchClient._next_request:
                _cancel(cancelled)
                time.sleep(max(0, min(.1, PublicResearchClient._next_request - time.monotonic())))
            PublicResearchClient._next_request = time.monotonic() + .15
        _cancel(cancelled)
        request = urllib.request.Request(url, headers={"User-Agent": "VerdictQuant desktop public research",
                    "X-Research-Project": PROJECT_CONTACT, "Accept": "application/json,application/rss+xml,text/html"})
        opener = urllib.request.build_opener(_IssuerRedirect(symbol))
        with opener.open(request, timeout=12) as response:
            _issuer_url(response.geturl(), symbol)
            if int(response.headers.get("Content-Length", "0")) >= MAX_BYTES:
                raise ValueError("ISSUER_RESPONSE_TOO_LARGE")
            raw = response.read(MAX_BYTES)
        _cancel(cancelled)
        if not raw.strip() or len(raw) >= MAX_BYTES:
            raise ValueError("ISSUER_RESPONSE_EMPTY_OR_TOO_LARGE")
        fetched = datetime.now(UTC).isoformat()
        source.update(status="ok", coverage=coverage, retrieved_at=fetched, fetched_at=fetched,
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
                source["cache_warning"] = "ISSUER_CACHE_WRITE_FAILED"
        return raw, source
    except CancelledError:
        raise
    except urllib.error.HTTPError as exc:
        source["error_code"] = "HTTP_" + str(exc.code)
    except (urllib.error.URLError, TimeoutError, OSError):
        source["error_code"] = "ISSUER_NETWORK_UNAVAILABLE"
    except (ValueError, TypeError):
        source["error_code"] = "ISSUER_RESPONSE_REJECTED"
    if raw_path:
        try:
            with _CACHE_LOCK:
                if raw_path.stat().st_size >= MAX_BYTES or meta_path.stat().st_size > 20_000:
                    raise ValueError("ISSUER_CACHE_TOO_LARGE")
                raw = raw_path.read_bytes()
                saved = json.loads(meta_path.read_text(encoding="utf-8"))
            retrieved = datetime.fromisoformat(saved["retrieved_at"])
            if (retrieved.tzinfo is None or retrieved > now or saved["url"] != url
                    or saved["sha256"] != hashlib.sha256(raw).hexdigest()):
                raise ValueError("ISSUER_CACHE_INVALID")
            source.update(status="stale", stale=True, coverage=coverage, retrieved_at=saved["retrieved_at"],
                          fetched_at=saved["retrieved_at"], sha256=saved["sha256"], bytes=len(raw),
                          cache_expired=now - retrieved > timedelta(hours=24))
            return raw, source
        except (OSError, KeyError, ValueError, TypeError):
            pass
    return None, source


def _publication(raw: str, at: datetime, rss: bool = False) -> tuple[str, str] | None:
    try:
        stamp = parsedate_to_datetime(raw) if rss else datetime.strptime(raw, "%m/%d/%Y %H:%M:%S")
        if stamp.tzinfo is not None:
            stamp = stamp.astimezone(UTC)
            if not at - timedelta(days=90) <= stamp <= at:
                return None
            return stamp.date().isoformat(), stamp.isoformat()
        # Q4 feed dates omit their timezone. Do not invent UTC or qualify today.
        if not at.date() - timedelta(days=90) <= stamp.date() < at.date():
            return None
        return stamp.date().isoformat(), stamp.date().isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def collect_issuer_news(symbol, as_of, cache_dir=None, cancelled=None) -> dict:
    """One official release excerpt; unsupported symbols never trigger networking.

    RSS/feed availability is partial coverage, not a complete risk review. A cached
    discovery feed cannot become fresh evidence merely because an article loads.
    """
    symbol = str(symbol).strip().upper()
    result = {"documents": [], "sources": [], "errors": [], "untrusted": True,
              "coverage": "issuer_excerpts_only_not_complete_risk_review"}
    if symbol not in HOSTS:
        result["errors"] = ["ISSUER_NEWS_UNSUPPORTED_SYMBOL"]
        return result
    if not isinstance(as_of, datetime) or as_of.tzinfo is None:
        raise ValueError("ISSUER_AS_OF_TIMEZONE_REQUIRED")
    at = as_of.astimezone(UTC)
    _cancel(cancelled)
    def get(url, coverage):
        raw, source = _request(url, symbol, coverage, cache_dir, cancelled)
        result["sources"].append(source)
        if source["status"] != "ok":
            result["errors"].append(source["id"] + ":" + str(source["error_code"]))
        return raw, source
    feed_url = NVIDIA_RSS if symbol == "NVDA" else _coin_feed(at.year)
    raw, feed_source = get(feed_url, "index" if symbol == "NVDA" else "excerpt")
    if raw is None:
        result["errors"].append("ISSUER_NEWS_NO_READABLE_RELEASE")
        return result
    try:
        candidates = []
        if symbol == "NVDA":
            if re.search(br"<!DOCTYPE|<!ENTITY", raw, re.I):
                raise ValueError("RSS_ENTITIES_REJECTED")
            for item in ET.fromstring(raw).findall(".//item"):
                title = str(item.findtext("title") or "").strip()
                published = _publication(item.findtext("pubDate"), at, rss=True)
                if not published or not re.search(r"financial results.*quarter|quarter.*financial results", title, re.I):
                    continue
                try:
                    url = _issuer_url(item.findtext("link"), symbol)
                except (ValueError, TypeError):
                    result["errors"].append("ISSUER_RELEASE_LINK_REJECTED")
                    continue
                candidates.append({"title": title, "url": url, "published": published})
            chosen = max(candidates, key=lambda r: r["published"][1], default=None)
            if chosen:
                body, body_source = get(chosen["url"], "excerpt")
                refs = [feed_source["id"], body_source["id"]]
        else:
            payload = json.loads(raw)
            for item in payload.get("GetPressReleaseListResult", []):
                title = str(item.get("Headline") or "").strip()
                published = _publication(item.get("PressReleaseDate"), at)
                if not published or not title or not isinstance(item.get("Body"), str) or not item["Body"].strip():
                    continue
                try:
                    url = _issuer_url(urllib.parse.urljoin("https://" + HOSTS[symbol], item.get("LinkToDetailPage", "")), symbol)
                except (ValueError, TypeError):
                    result["errors"].append("ISSUER_RELEASE_LINK_REJECTED")
                    continue
                earnings = bool(re.search(r"(?:Q[1-4]|quarter).*earnings|earnings.*quarter|reports.*(?:quarter|results)", title, re.I))
                # A date announcement is not the quarterly results release itself.
                earnings = earnings and not re.search(r"announces date|to (?:report|release|post)", title, re.I)
                candidates.append({"title": title, "url": url, "published": published, "body": item["Body"], "earnings": earnings})
            chosen = max(candidates, key=lambda r: (r["earnings"], r["published"][0]), default=None)
            if chosen:
                body, body_source, refs = chosen["body"].encode("utf-8"), feed_source, [feed_source["id"]]
        if not chosen or not body:
            result["errors"].append("ISSUER_NEWS_NO_RECENT_RELEASE_IN_FEED")
            return result
        _cancel(cancelled)
        text, _ = _extract(body)
        # Strip preceding site furniture if the headline is present in the article.
        title_at = text.casefold().find(chosen["title"].casefold())
        if title_at >= 0:
            text = text[title_at:]
        excerpt, offset = _excerpt(text)
        if not excerpt.strip():
            result["errors"].append("ISSUER_RELEASE_NO_VISIBLE_TEXT")
            return result
        body_source["publication_date"], body_source["published_at"] = chosen["published"]
        result["documents"].append({"title": chosen["title"][:500], "text": excerpt, "url": chosen["url"],
            "source_id": body_source["id"], "source_refs": refs, "publication_date": chosen["published"][0],
            "published_at": chosen["published"][1], "publication_timezone_verified": symbol == "NVDA" and "T" in chosen["published"][1],
            "retrieved_at": body_source["retrieved_at"], "sha256": body_source["sha256"], "coverage": "excerpt",
            "stale": feed_source["stale"] or body_source["stale"], "untrusted": True, "complete_risk_review": False,
            "excerpt_start_char": offset, "truncated": offset > 0 or len(text) > len(excerpt)})
    except (ValueError, TypeError, KeyError, AttributeError, ET.ParseError):
        result["errors"].append("ISSUER_FEED_OR_BODY_INVALID")
    _cancel(cancelled)
    return result
