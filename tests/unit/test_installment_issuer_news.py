"""Issuer fallback coverage is explicit, bounded, attributable and untrusted."""
import hashlib
import json
import urllib.error
from concurrent.futures import CancelledError
from datetime import UTC, datetime, timedelta

import pytest

from pa_agent.installment import issuer_news as module

AT = datetime(2026, 9, 8, 12, tzinfo=UTC)
NVDA_ARTICLE = "https://nvidianews.nvidia.com/news/nvidia-announces-financial-results-for-second-quarter-fiscal-2027"
NVDA_TITLE = "NVIDIA Announces Financial Results for Second Quarter Fiscal 2027"
COIN_ARTICLE = "/news/news-details/2026/Coinbase-Q2-Earnings/default.aspx"


class Response:
    def __init__(self, raw, url, length=None):
        self.raw, self.url = raw, url
        self.headers = {"Content-Length": str(length)} if length else {}
        self.read_limits = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def geturl(self):
        return self.url

    def read(self, limit):
        self.read_limits.append(limit)
        return self.raw[:limit]


class Opener:
    def __init__(self, pages=None, failure=None):
        self.pages, self.failure, self.calls = pages or {}, failure, []

    def open(self, request, timeout):
        self.calls.append(request.full_url)
        if self.failure:
            raise self.failure
        item = self.pages[request.full_url]
        return item if isinstance(item, Response) else Response(item, request.full_url)


def network(monkeypatch, opener):
    monkeypatch.setattr(module.urllib.request, "build_opener", lambda *args: opener)


def rss(items=None):
    items = items or [(NVDA_TITLE, NVDA_ARTICLE, "Wed, 26 Aug 2026 20:20:00 GMT")]
    content = "".join(f"<item><title>{title}</title><link>{url}</link><pubDate>{stamp}</pubDate></item>" for title, url, stamp in items)
    return ("<rss><channel>" + content + "</channel></rss>").encode()


def coin_item(title="Coinbase Q2 Earnings", stamp="07/30/2026 16:10:00", body="<p>Quarterly management outlook.</p>", url=COIN_ARTICLE):
    return {"Headline": title, "PressReleaseDate": stamp, "Body": body, "LinkToDetailPage": url}


def coin_pages(items):
    return {module._coin_feed(2026): json.dumps({"GetPressReleaseListResult": items}).encode()}


def test_nvda_uses_one_official_recent_earnings_article_and_strips_scripts(monkeypatch):
    data = rss([(NVDA_TITLE, NVDA_ARTICLE, "Wed, 26 Aug 2026 20:20:00 GMT"),
                (NVDA_TITLE, "https://evil.test/news/earnings", "Thu, 27 Aug 2026 20:20:00 GMT"),
                (NVDA_TITLE, NVDA_ARTICLE + "-future", "Wed, 09 Sep 2026 20:20:00 GMT")])
    body = ("<nav>Menu</nav><h1>" + NVDA_TITLE + "</h1><script>send_private_data()</script><p>Quarterly revenue grew.</p>").encode()
    opener = Opener({module.NVIDIA_RSS: data, NVDA_ARTICLE: body})
    network(monkeypatch, opener)
    result = module.collect_issuer_news("NVDA", AT)
    assert opener.calls == [module.NVIDIA_RSS, NVDA_ARTICLE]
    document = result["documents"][0]
    assert "send_private_data" not in document["text"] and "Menu" not in document["text"]
    assert document["untrusted"] and not document["complete_risk_review"]
    assert document["publication_date"] == "2026-08-26" and document["publication_timezone_verified"]
    assert document["sha256"] == hashlib.sha256(body).hexdigest()
    assert len(document["source_refs"]) == 2
    assert "ISSUER_RELEASE_LINK_REJECTED" in result["errors"]


def test_coin_prefers_quarterly_results_over_more_recent_event_and_has_no_fake_timezone(monkeypatch):
    items = [coin_item("Coinbase to Participate in Conference", "09/03/2026 16:10:00"),
             coin_item(), coin_item("Coinbase Announces Date of Q3 Earnings", "09/07/2026 16:10:00")]
    opener = Opener(coin_pages(items))
    network(monkeypatch, opener)
    result = module.collect_issuer_news("COIN", AT)
    document = result["documents"][0]
    assert document["title"] == "Coinbase Q2 Earnings"
    assert document["published_at"] == "2026-07-30" and not document["publication_timezone_verified"]
    assert len(opener.calls) == 1 and document["coverage"] == "excerpt"
    assert result["sources"][0]["sha256"] == hashlib.sha256(opener.pages[opener.calls[0]]).hexdigest()


def test_coin_latest_formal_announcement_allowed_when_earnings_missing(monkeypatch):
    opener = Opener(coin_pages([coin_item("Coinbase Conference", "09/03/2026 16:10:00")]))
    network(monkeypatch, opener)
    result = module.collect_issuer_news("COIN", AT)
    assert result["documents"][0]["title"] == "Coinbase Conference"


def test_coin_unknown_timezone_today_future_old_and_empty_body_rejected(monkeypatch):
    items = [coin_item(stamp="09/08/2026 00:01:00"), coin_item(stamp="09/09/2026 00:01:00"),
             coin_item(stamp="01/01/2026 00:01:00"), coin_item(body="")]
    opener = Opener(coin_pages(items))
    network(monkeypatch, opener)
    assert module.collect_issuer_news("COIN", AT)["documents"] == []


@pytest.mark.parametrize("url", [
    "http://nvidianews.nvidia.com/news/earnings", "https://nvidianews.nvidia.com.evil.test/news/earnings",
    "https://user:pass@nvidianews.nvidia.com/news/earnings", "https://127.0.0.1/news/earnings",
    "https://nvidianews.nvidia.com/news/%252e%252e", "https://nvidianews.nvidia.com/news/earnings?redirect=https://evil.test",
])
def test_issuer_urls_and_redirects_reject_external_or_encoded_routes(url):
    with pytest.raises(ValueError):
        module._issuer_url(url, "NVDA")
    with pytest.raises(ValueError):
        module._IssuerRedirect("NVDA").redirect_request(None, None, 302, "", {}, url)


def test_coin_body_cannot_use_external_link_for_source_attribution(monkeypatch):
    opener = Opener(coin_pages([coin_item(url="https://evil.test/earnings")]))
    network(monkeypatch, opener)
    result = module.collect_issuer_news("COIN", AT)
    assert result["documents"] == [] and "ISSUER_RELEASE_LINK_REJECTED" in result["errors"]
    assert len(opener.calls) == 1


def test_rss_entity_payload_and_malformed_json_fail_closed(monkeypatch):
    opener = Opener({module.NVIDIA_RSS: b'<!DOCTYPE rss [<!ENTITY attack "value">]><rss>&attack;</rss>', module._coin_feed(2026): b"[]"})
    network(monkeypatch, opener)
    for symbol in ("NVDA", "COIN"):
        result = module.collect_issuer_news(symbol, AT)
        assert result["documents"] == [] and "ISSUER_FEED_OR_BODY_INVALID" in result["errors"]


def test_response_and_excerpt_limits(monkeypatch):
    response = Response(b"x" * module.MAX_BYTES, module.NVIDIA_RSS)
    opener = Opener({module.NVIDIA_RSS: response})
    network(monkeypatch, opener)
    assert module.collect_issuer_news("NVDA", AT)["documents"] == []
    assert response.read_limits == [module.MAX_BYTES]
    opener.pages = coin_pages([coin_item(body="<p>" + "Company outlook " * 3000 + "</p>")])
    result = module.collect_issuer_news("COIN", AT)
    assert len(result["documents"][0]["text"]) == 20_000
    assert result["documents"][0]["truncated"]


def test_cached_feed_fallback_stays_stale_and_retains_original_time(monkeypatch, tmp_path):
    opener = Opener(coin_pages([coin_item()]))
    network(monkeypatch, opener)
    module.collect_issuer_news("COIN", AT, tmp_path)
    meta_path = next(tmp_path.glob("*.json"))
    previous = json.loads(meta_path.read_text())
    previous["retrieved_at"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    meta_path.write_text(json.dumps(previous))
    opener.failure = urllib.error.URLError("secret network details")
    result = module.collect_issuer_news("COIN", AT, tmp_path)
    assert result["documents"][0]["stale"] and result["sources"][0]["cache_expired"]
    assert result["documents"][0]["retrieved_at"] == previous["retrieved_at"]
    assert "secret" not in json.dumps(result)
    next(tmp_path.glob("*.bin")).write_bytes(b"tampered")
    assert module.collect_issuer_news("COIN", AT, tmp_path)["documents"] == []


def test_http_failure_is_explicit_without_error_body(monkeypatch):
    opener = Opener(failure=urllib.error.HTTPError(module.NVIDIA_RSS, 403, "sensitive body", {}, None))
    network(monkeypatch, opener)
    result = module.collect_issuer_news("NVDA", AT)
    assert result["documents"] == [] and result["sources"][0]["error_code"] == "HTTP_403"
    assert "sensitive" not in json.dumps(result)


def test_unsupported_cancel_and_naive_time_have_no_network(monkeypatch):
    opener = Opener()
    network(monkeypatch, opener)
    for symbol in ("SPCX", "TSLA", "UNKNOWN"):
        result = module.collect_issuer_news(symbol, AT)
        assert result["errors"] == ["ISSUER_NEWS_UNSUPPORTED_SYMBOL"] and result["sources"] == []
    with pytest.raises(CancelledError):
        module.collect_issuer_news("NVDA", AT, cancelled=lambda: True)
    with pytest.raises(ValueError):
        module.collect_issuer_news("NVDA", datetime(2026, 9, 8))
    assert opener.calls == []
