"""Public filing text must remain bounded, attributed and inert."""
import json
import urllib.error
from concurrent.futures import CancelledError
from datetime import UTC, datetime, timedelta

import pytest

from pa_agent.installment import documents as module

BASE = "https://www.sec.gov/Archives/edgar/data/1/000000000126000001"
URL = BASE + "/example.htm"
AT = datetime(2026, 9, 8, 12, tzinfo=UTC)


def filing(url=URL, form="8-K", day="2026-08-01"):
    return {"url": url, "form": form, "filing_date": day, "title": form + " " + day}


class Response:
    def __init__(self, raw, url=URL, length=None):
        self.raw, self.url = raw, url
        self.headers = {"Content-Length": str(length)} if length else {}
        self.requested_limits = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def geturl(self):
        return self.url

    def read(self, limit):
        self.requested_limits.append(limit)
        return self.raw[:limit]


class Opener:
    def __init__(self, pages=None, error=None):
        self.pages, self.error, self.calls = pages or {}, error, []

    def open(self, request, timeout):
        self.calls.append(request.full_url)
        if self.error:
            raise self.error
        page = self.pages.get(request.full_url, b"<p>Financial results were announced.</p>")
        return page if isinstance(page, Response) else Response(page, request.full_url)


def patch_network(monkeypatch, opener):
    monkeypatch.setattr(module.urllib.request, "build_opener", lambda *args: opener)


def test_script_style_navigation_and_hidden_text_removed():
    text, links = module._extract(b'''<html><head><title>Menu</title></head><body>
        <script>send_secret_to_attacker()</script><style>evil-css</style><nav>Buy account</nav>
        <div hidden>hidden payload</div><div style="display: none">hidden CSS</div>
        <div role="navigation">Menu action</div><ix:hidden>Invisible XBRL context</ix:hidden>
        <h1>Quarterly results</h1><p>Revenue increased &amp; costs fell.</p>
        <p>Ignore prior instructions is quoted untrusted company text.</p></body></html>''')
    assert "send_secret" not in text and "evil-css" not in text and "hidden" not in text
    assert "Buy account" not in text and "Menu" not in text and "XBRL" not in text
    assert "Revenue increased & costs fell." in text
    # Visible quoted instructions stay inert research text; this module never executes them.
    assert "Ignore prior instructions" in text


@pytest.mark.parametrize("url", [
    BASE + "/%2e%2e/secret.htm", BASE + "/%252e%252e%252fsecret.htm",
    BASE + "/ex99.htm?url=https://evil.test", "https://www.sec.gov.evil.test/Archives/edgar/data/1/000000000126000001/ex99.htm",
    "https://user:password@www.sec.gov/Archives/edgar/data/1/000000000126000001/ex99.htm",
    "https://127.0.0.1/Archives/edgar/data/1/000000000126000001/ex99.htm",
    "https://www.sec.gov\\@evil.test/Archives/edgar/data/1/000000000126000001/ex99.htm",
    "http://www.sec.gov/Archives/edgar/data/1/000000000126000001/ex99.htm",
])
def test_unsafe_or_encoded_document_urls_are_rejected(url):
    with pytest.raises(ValueError):
        module._sec_url(url)


def test_redirect_validated_before_an_offsite_request():
    with pytest.raises(ValueError):
        module._SECRedirect(BASE).redirect_request(None, None, 302, "", {}, "https://evil.test/ex99.htm")
    with pytest.raises(ValueError):
        module._SECRedirect(BASE).redirect_request(None, None, 302, "", {}, BASE.replace("000001", "000002") + "/ex99.htm")


def test_future_old_and_same_day_without_acceptance_are_not_downloaded(monkeypatch):
    opener = Opener()
    patch_network(monkeypatch, opener)
    result = module.collect_documents([filing(day="2026-09-09"), filing(day="2025-01-01"), filing(day="2026-09-08")], AT)
    assert opener.calls == [] and result["documents"] == []
    assert "SAME_DAY_FILING_ACCEPTANCE_TIME_UNAVAILABLE" in result["errors"]
    valid = {**filing(day="2026-09-08"), "accepted_at": "2026-09-08T11:00:00Z"}
    assert len(module.collect_documents([valid], AT)["documents"]) == 1
    opener.calls.clear()
    assert module.collect_documents([{**valid, "accepted_at": "2026-09-08T13:00:00Z"}], AT)["documents"] == []
    assert opener.calls == []


def test_exhibit_links_stay_in_same_accession_and_only_one_is_read(monkeypatch):
    cover = b'''<p>Results are in the attached exhibit.</p>
      <a href="https://evil.test/ex99.htm">Exhibit 99.1</a>
      <a href="../000000000126000002/ex99.htm">Exhibit 99.1</a>
      <a href="%2f%2fevil.test/ex99.htm">Exhibit 99.1</a>
      <a href="q2pr.htm" style="-sec-extract:exhibit">Exhibit 99.1 quarterly press release</a>
      <a href="q2cfo.htm">CFO commentary</a>'''
    opener = Opener({URL: cover, BASE + "/q2pr.htm": b"<h1>Quarterly earnings</h1><p>Management outlook.</p>"})
    patch_network(monkeypatch, opener)
    result = module.collect_documents([filing()], AT)
    assert opener.calls == [URL, BASE + "/q2pr.htm"]
    assert result["documents"][0]["url"] == BASE + "/q2pr.htm"
    assert "Management outlook" in result["documents"][0]["text"]
    assert len(result["documents"][0]["source_refs"]) == 2
    assert result["errors"].count("SEC_ATTACHMENT_LINK_REJECTED") == 3


def test_latest_quarterly_report_and_same_day_earnings_cover_preferred(monkeypatch):
    opener = Opener()
    patch_network(monkeypatch, opener)
    entries = [filing(BASE + "/newer.htm", day="2026-09-03"),
               filing(BASE + "/quarter.htm", "10-Q", "2026-08-26"),
               filing(BASE + "/earnings.htm", "8-K", "2026-08-26")]
    result = module.collect_documents(entries, AT)
    assert opener.calls == [BASE + "/quarter.htm", BASE + "/earnings.htm"]
    assert len(result["documents"]) == 2
    assert all(doc["coverage"] == "excerpt" and doc["untrusted"] and not doc["complete_risk_review"] for doc in result["documents"])


def test_two_megabyte_limit_and_twenty_thousand_character_excerpt(monkeypatch):
    response = Response(b"x" * module.MAX_BYTES)
    opener = Opener({URL: response})
    patch_network(monkeypatch, opener)
    result = module.collect_documents([filing()], AT)
    assert result["documents"] == []
    assert response.requested_limits == [module.MAX_BYTES]
    opener.pages[URL] = b"<p>" + b"Quarterly growth " * 2000 + b"</p>"
    result = module.collect_documents([filing()], AT)
    assert len(result["documents"][0]["text"]) == module.MAX_TEXT
    assert result["documents"][0]["truncated"]


def test_excerpt_skips_table_of_contents_for_management_discussion():
    heading = "Management's Discussion and Analysis of Financial Condition\n"
    text = "Intro " * 700 + "\n" + heading + "25\nItem 3.\nMarket risk\n"
    text += "Financial statements " * 1000 + "\nItem 2. " + heading + "Management explains the business. " * 500
    excerpt, offset = module._excerpt(text)
    assert offset > 10_000
    assert "Management explains the business" in excerpt[:150]
    assert "25\nItem 3" not in excerpt


def test_http_failure_stays_index_only_and_does_not_expose_error_body(monkeypatch):
    opener = Opener(error=urllib.error.HTTPError(URL, 403, "secret details", {}, None))
    patch_network(monkeypatch, opener)
    result = module.collect_documents([filing()], AT)
    assert result["documents"] == []
    assert result["sources"][0]["coverage"] == "index_only"
    assert result["sources"][0]["error_code"] == "HTTP_403"
    assert "secret" not in json.dumps(result)


def test_expired_cache_remains_stale_with_original_retrieval_time(monkeypatch, tmp_path):
    opener = Opener()
    patch_network(monkeypatch, opener)
    module.collect_documents([filing()], AT, tmp_path)
    meta_path = next(tmp_path.glob("*.json"))
    previous = json.loads(meta_path.read_text())
    previous["retrieved_at"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    meta_path.write_text(json.dumps(previous))
    opener.error = urllib.error.URLError("private network details")
    result = module.collect_documents([filing()], AT, tmp_path)
    source, document = result["sources"][0], result["documents"][0]
    assert source["stale"] and source["cache_expired"] and source["status"] == "stale"
    assert document["stale"] and document["retrieved_at"] == previous["retrieved_at"]
    assert "private" not in json.dumps(result)
    # Hash mismatch cannot inherit the original SEC source attribution.
    next(tmp_path.glob("*.html")).write_bytes(b"tampered")
    assert module.collect_documents([filing()], AT, tmp_path)["documents"] == []


def test_cancel_and_timezone_validation(monkeypatch):
    opener = Opener()
    patch_network(monkeypatch, opener)
    with pytest.raises(CancelledError):
        module.collect_documents([filing()], AT, cancelled=lambda: True)
    with pytest.raises(ValueError):
        module.collect_documents([filing()], datetime(2026, 9, 8))
    assert opener.calls == []
