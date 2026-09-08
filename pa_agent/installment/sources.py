"""Bounded, public-only research sources. No accounts, recommendations or trading.

SEC facts are issuer accounting facts, not automatically ADR/per-share valuation inputs.
Every value retains its unit, period, filing and source. Unavailable is never zero.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import CancelledError
from datetime import UTC, date, datetime, time as daytime, timedelta
from pathlib import Path


ALLOWED_HOSTS = frozenset({"www.sec.gov", "data.sec.gov", "query1.finance.yahoo.com"})
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
TICKERS_EXCHANGE_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
PROJECT_CONTACT = "https://github.com/Monkey-Sheep/VerdictQuant"
# Routing hints observed in SEC's official ticker index on 2026-09-08. These are
# never sufficient identity evidence: every fallback requires live submissions
# to confirm the CIK, exact ticker and normalized issuer name again.
IDENTITY_SEEDS = {
    "NVDA": (1045810, "NVIDIA CORP"),
    "TSLA": (1318605, "Tesla, Inc."),
    "SPCX": (1181412, "SPACE EXPLORATION TECHNOLOGIES CORP"),
    "COIN": (1679788, "Coinbase Global, Inc."),
    "TSM": (1046179, "TAIWAN SEMICONDUCTOR MANUFACTURING CO LTD"),
    "ASML": (937966, "ASML HOLDING NV"),
    "MU": (723125, "MICRON TECHNOLOGY INC"),
    "CEG": (1868275, "Constellation Energy Corp"),
    "VST": (1692819, "Vistra Corp."),
    "SNDK": (2023554, "Sandisk Corp"),
    "SKHY": (2120882, "SK hynix Inc."),
}
TAGS = {
    "revenue": ("Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet", "Revenue"),
    "net_income": ("NetIncomeLoss", "ProfitLossAttributableToOwnersOfParent", "ProfitLoss"),
    "operating_income": ("OperatingIncomeLoss", "ProfitLossFromOperatingActivities"),
    "operating_cash_flow": ("NetCashProvidedByUsedInOperatingActivities", "CashFlowsFromUsedInOperatingActivities"),
    "capex": ("PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets", "PurchaseOfPropertyPlantAndEquipment", "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"),
    "cash": ("CashAndCashEquivalentsAtCarryingValue", "CashAndCashEquivalents"),
    # Never substitute long-term debt, liabilities or debt plus lease liabilities.
    "debt": ("DebtCurrentAndNoncurrent", "LongTermDebtAndShortTermBorrowings"),
    "shares_diluted": ("WeightedAverageNumberOfDilutedSharesOutstanding", "AdjustedWeightedAverageShares"),
    "eps": ("EarningsPerShareDiluted", "DilutedEarningsLossPerShare"),
}
INSTANT = {"cash", "debt"}
PER_SHARE = {"eps", "shares_diluted"}
FORMS = {"10-K", "10-K/A", "10-Q", "10-Q/A", "20-F", "20-F/A", "40-F", "40-F/A", "6-K", "8-K", "S-1", "S-1/A", "F-1", "F-1/A", "424B4"}
VALUATION_TYPES = {"market_cap": "trailingMarketCap", "pe": "trailingPeRatio", "ps": "trailingPsRatio",
                   "enterprise_value": "trailingEnterpriseValue", "ev_revenue": "trailingEnterprisesValueRevenueRatio"}
VENDOR_TYPES = {"revenue": "TotalRevenue", "net_income": "NetIncome", "operating_cash_flow": "OperatingCashFlow",
                "capex": "CapitalExpenditure", "eps": "DilutedEPS"}


def _cutoff(value: date | datetime) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("AS_OF_TIMEZONE_REQUIRED")
        return value.astimezone(UTC)
    if isinstance(value, date):
        return datetime.combine(value, daytime.max, UTC)
    raise ValueError("AS_OF_DATE_REQUIRED")


def _cancel(callback) -> None:
    if callback and callback():
        raise CancelledError()


def _safe_url(value: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(value)
        return parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password
    except (TypeError, ValueError):
        return False


def _public_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if not _safe_url(url) or parsed.hostname not in ALLOWED_HOSTS or parsed.port not in (None, 443):
        raise ValueError("PUBLIC_SOURCE_NOT_ALLOWED")


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _entity_key(value) -> str:
    words = re.findall(r"[a-z0-9]+", str(value).lower())
    legal = {"inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited"}
    return " ".join(word for word in words if word not in legal)


def _ticker_rows(payload: dict | None) -> list[dict] | None:
    if not isinstance(payload, dict):
        return None
    if "fields" in payload and "data" in payload:
        fields = payload.get("fields")
        if not isinstance(fields, list) or not all(key in fields for key in ("cik", "name", "ticker")):
            return None
        rows = []
        for values in payload.get("data", []):
            if not isinstance(values, list) or len(values) != len(fields):
                continue
            item = dict(zip(fields, values))
            rows.append({"cik_str": item["cik"], "title": item["name"], "ticker": item["ticker"]})
        return rows or None
    rows = [row for row in payload.values() if isinstance(row, dict)
            and all(key in row for key in ("cik_str", "ticker", "title"))]
    return rows or None


def _value(row: dict, unit: str, tag: str, source_id: str, basis: str = "reported") -> dict:
    return {"value": row["val"], "unit": unit, "period_start": row.get("start"),
            "period_end": row["end"], "filing_date": row["filed"],
            "accession": row.get("accn"), "source_refs": [source_id],
            "basis": basis, "tag": tag}


def _days(row: dict) -> int:
    return (date.fromisoformat(row["end"]) - date.fromisoformat(row["start"])).days + 1


def _quarter_change(current: dict, prior: dict, basis: str) -> tuple[dict | None, dict | None]:
    """Comparable quarter arithmetic only; retain both reported observations."""
    components = [dict(current), dict(prior)]
    refs = list(dict.fromkeys(current["source_refs"] + prior["source_refs"]))
    base, value = prior["value"], current["value"]
    if base > 0:
        change = (value / base - 1) * 100
        if not _number(change):
            return None, None
        return {"value": change, "unit": "percent", "period_start": current.get("period_start"),
                "period_end": current["period_end"],
                "filing_date": max(current["filing_date"], prior["filing_date"]) if current.get("filing_date") and prior.get("filing_date") else None,
                "source_refs": refs, "components": components,
                "basis": "(current quarter / prior-year same quarter - 1) * 100; no annualization; " + basis}, None
    if base < 0:
        transition = ("loss_to_profit" if value > 0 else "loss_to_breakeven" if value == 0
                      else "loss_narrowed" if value > base else "loss_widened" if value < base else "unchanged_loss")
    else:
        transition = "zero_to_profit" if value > 0 else "zero_to_loss" if value < 0 else "unchanged_zero"
    return None, {"status": transition, "source_refs": refs, "components": components,
                  "basis": "prior-year quarter is nonpositive; percentage growth is not defined; " + basis}


def extract_financials(payload: dict, as_of: date | datetime, source_id: str,
                       accepted: dict | None = None) -> dict:
    """Extract standard tags, preserving units; conservative annual/YTD TTM only.

    Same-day facts require an acceptance timestamp for datetime cutoffs. A date
    cutoff intentionally includes the full UTC day. Restatements filed later are
    excluded. EPS and diluted shares are never added/subtracted across filings.
    """
    at = _cutoff(as_of)
    cutoff_day = at.date().isoformat()
    metrics, warnings = {}, []
    for metric, aliases in TAGS.items():
        candidates = []
        for taxonomy in ("us-gaap", "ifrs-full"):
            for rank, tag in enumerate(aliases):
                body = payload.get("facts", {}).get(taxonomy, {}).get(tag, {})
                for unit, rows in body.get("units", {}).items():
                    if metric == "shares_diluted" and unit != "shares":
                        continue
                    if metric == "eps" and not re.fullmatch(r"[A-Z]{3}/shares", unit):
                        continue
                    if metric not in PER_SHARE and not re.fullmatch(r"[A-Z]{3}", unit):
                        continue
                    valid = []
                    for row in rows:
                        try:
                            end, filed = date.fromisoformat(row["end"]), date.fromisoformat(row["filed"])
                            if not _number(row.get("val")) or row.get("form") not in FORMS:
                                continue
                            if end > at.date() or filed > at.date():
                                continue
                            if isinstance(as_of, datetime) and row["filed"] == cutoff_day:
                                timestamp = (accepted or {}).get(row.get("accn"))
                                if not timestamp or datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(UTC) > at:
                                    continue
                            if metric not in INSTANT and not (1 <= _days(row) <= 380):
                                continue
                            valid.append(row)
                        except (KeyError, TypeError, ValueError):
                            continue
                    if valid:
                        candidates.append((max(r["end"] for r in valid), -rank, taxonomy + ":" + tag, unit, valid))
        output = {"annual": None, "latest_quarter": None, "ttm": None, "latest": None, "warnings": []}
        if metric in {"revenue", "net_income"}:
            output.update(prior_year_quarter=None, quarter_yoy_pct=None, profit_transition=None)
        if not candidates:
            output["warnings"].append("STANDARD_FACTS_MISSING")
            metrics[metric] = output
            continue
        newest = max(c[0] for c in candidates)
        best_rank = max(c[1] for c in candidates if c[0] == newest)
        choices = [c for c in candidates if c[0] == newest and c[1] == best_rank]
        if len({c[3] for c in choices}) != 1:
            output["warnings"].append("MULTIPLE_REPORTING_UNITS_REQUIRES_REVIEW")
            output["available_units"] = sorted({c[3] for c in choices})
            metrics[metric] = output
            continue
        _, _, tag, unit, rows = choices[0]
        # Latest filed representation for an identical period, no dimensional summation.
        periods = {}
        ambiguous = set()
        for row in sorted(rows, key=lambda r: (r["filed"], r.get("accn", ""))):
            key = row.get("start"), row["end"]
            prior = periods.get(key)
            if prior and prior["filed"] == row["filed"] and prior.get("accn") == row.get("accn") and prior["val"] != row["val"]:
                ambiguous.add(key)
            elif prior and prior["filed"] != row["filed"]:
                ambiguous.discard(key)
            periods[key] = row
        rows = [r for key, r in periods.items() if key not in ambiguous]
        if ambiguous:
            output["warnings"].append("CONFLICTING_SAME_PERIOD_FACTS_EXCLUDED")
        order = lambda r: (r["end"], r["filed"])
        annuals = [r for r in rows if metric not in INSTANT and 350 <= _days(r) <= 380]
        annual = max(annuals, key=order, default=None)
        if metric in INSTANT:
            instant_rows = [r for r in rows if not r.get("start")]
            latest = max(instant_rows, key=order, default=None)
            if latest:
                output["latest"] = _value(latest, unit, tag, source_id, "reported balance at period end")
        else:
            if annual:
                output["annual"] = _value(annual, unit, tag, source_id)
            quarters = [r for r in rows if 70 <= _days(r) <= 110]
            latest_quarter = max(quarters, key=order, default=None)
            if latest_quarter:
                output["latest_quarter"] = _value(latest_quarter, unit, tag, source_id)
                if metric in {"revenue", "net_income"}:
                    comparable = [r for r in quarters if abs(_days(r) - _days(latest_quarter)) <= 1
                                  and 350 <= (date.fromisoformat(latest_quarter["end"]) - date.fromisoformat(r["end"])).days <= 380]
                    if len(comparable) == 1:
                        output["prior_year_quarter"] = _value(comparable[0], unit, tag, source_id)
                        output["quarter_yoy_pct"], transition = _quarter_change(
                            output["latest_quarter"], output["prior_year_quarter"],
                            "SEC latest disclosed versions; same tag/unit; quarter durations differ by at most one day")
                        if metric == "net_income":
                            output["profit_transition"] = transition
            if metric not in PER_SHARE and annual:
                interim = max([r for r in rows if r["end"] > annual["end"] and 70 <= _days(r) <= 300
                               and 0 < (date.fromisoformat(r["start"]) - date.fromisoformat(annual["end"])).days <= 8],
                              key=order, default=None)
                if interim:
                    comparative = [r for r in rows if r.get("accn") == interim.get("accn")
                                   and r["start"] == annual["start"]
                                   and abs(_days(r) - _days(interim)) <= 8
                                   and 350 <= (date.fromisoformat(interim["end"]) - date.fromisoformat(r["end"])).days <= 380]
                    if len(comparative) == 1:
                        prior = comparative[0]
                        value = _value(interim, unit, tag, source_id, "annual + current YTD - prior YTD; matching tag/unit and comparative accession")
                        value.update(value=annual["val"] + interim["val"] - prior["val"],
                                     period_start=(date.fromisoformat(prior["end"]) + timedelta(days=1)).isoformat(),
                                     filing_date=max(annual["filed"], interim["filed"], prior["filed"]))
                        value["components"] = [_value(r, unit, tag, source_id) for r in (annual, interim, prior)]
                        output["ttm"] = value
                    else:
                        output["warnings"].append("TTM_COMPARATIVE_MISSING")
                elif newest == annual["end"]:
                    output["ttm"] = _value(annual, unit, tag, source_id, "reported full year ending on latest available period")
            if metric in PER_SHARE:
                output["warnings"].append("PER_SHARE_OR_ADR_BASIS_UNVERIFIED_NO_DERIVED_TTM")
        if (at.date() - date.fromisoformat(newest)).days > 180:
            output["warnings"].append("FINANCIAL_PERIOD_OLDER_THAN_180_DAYS")
        if output["latest_quarter"] and output["latest_quarter"]["period_end"] < newest:
            output["warnings"].append("STANDALONE_QUARTER_OLDER_THAN_LATEST_REPORTED_PERIOD")
        for key in ("annual", "latest_quarter", "ttm", "latest", "quarter_yoy_pct"):
            if output.get(key):
                age = (at.date() - date.fromisoformat(output[key]["period_end"])).days
                output[key].update(age_days=age, stale=age > 180)
        output["tag"], output["unit"] = tag, unit
        metrics[metric] = output
    warnings.append("STANDARD_SEC_FACTS_ONLY_NOT_A_COMPLETE_EARNINGS_OR_FORWARD_GUIDANCE_REVIEW")
    return {"metrics": metrics, "warnings": warnings}


def extract_timeseries(payload: dict, symbol: str, as_of: date | datetime, source_id: str) -> tuple[dict, dict]:
    """Vendor current snapshot only: asOfDate is a period, never publication time."""
    at = _cutoff(as_of)
    warning = "VENDOR_CURRENT_SNAPSHOT_NOT_POINT_IN_TIME_FILING_EVIDENCE"
    valuation = {"metrics": {}, "warnings": [warning], "point_in_time_verified": False}
    financials = {"metrics": {}, "warnings": [warning, "ADR_AND_PER_SHARE_BASIS_UNVERIFIED"], "point_in_time_verified": False}
    series = {}
    for item in (payload.get("timeseries", {}).get("result") or []):
        if not isinstance(item, dict) or item.get("meta", {}).get("symbol") != [symbol]:
            continue
        for kind in item.get("meta", {}).get("type", []):
            valid = []
            for row in item.get(kind, []):
                try:
                    day = date.fromisoformat(row["asOfDate"])
                    if day > at.date() or not _number(row.get("reportedValue", {}).get("raw")):
                        continue
                    valid.append(row)
                except (KeyError, TypeError, ValueError):
                    continue
            series[kind] = valid
    for name, kind in VALUATION_TYPES.items():
        rows = [r for r in series.get(kind, []) if date.fromisoformat(r["asOfDate"]) >= at.date() - timedelta(days=180)]
        row = max(rows, key=lambda r: r["asOfDate"], default=None)
        value = None
        if row:
            unit = "ratio" if name in {"pe", "ps", "ev_revenue"} else row.get("currencyCode")
            if unit and (unit == "ratio" or re.fullmatch(r"[A-Z]{3}", unit)):
                value = {"value": row["reportedValue"]["raw"], "as_of": row["asOfDate"], "unit": unit,
                         "source_refs": [source_id], "provider": "Yahoo Finance", "basis": "vendor reported; ADR/denominator not independently verified"}
            else:
                valuation["warnings"].append(name + ":CURRENCY_MISSING")
        valuation["metrics"][name] = value
    for name, suffix in VENDOR_TYPES.items():
        metric = {"annual": None, "latest_quarter": None, "ttm": None}
        if name in {"revenue", "net_income"}:
            metric.update(prior_year_quarter=None, quarter_yoy_pct=None, profit_transition=None)
        for output, prefix in (("annual", "annual"), ("latest_quarter", "quarterly"), ("ttm", "trailing")):
            row = max(series.get(prefix + suffix, []), key=lambda r: r["asOfDate"], default=None)
            if row and re.fullmatch(r"[A-Z]{3}", str(row.get("currencyCode", ""))):
                metric[output] = {"value": row["reportedValue"]["raw"], "unit": row["currencyCode"] + ("/shares" if name == "eps" else ""),
                                  "period_start": None, "period_end": row["asOfDate"], "filing_date": None,
                                  "period_type": row.get("periodType"), "source_refs": [source_id], "provider": "Yahoo Finance",
                                  "basis": "vendor current snapshot; publication date unavailable; not backtest evidence"}
        if name in {"revenue", "net_income"} and metric["latest_quarter"]:
            current = metric["latest_quarter"]
            if current["period_type"] == "3M":
                # Vendor data does not disclose filing time or precise start dates.
                # Use its 3M period label, and reject conflicting duplicate periods.
                quarter_rows = [r for r in series.get("quarterly" + suffix, [])
                                if r.get("periodType") == "3M" and r.get("currencyCode") == current["unit"]]
                current_values = {r["reportedValue"]["raw"] for r in quarter_rows if r["asOfDate"] == current["period_end"]}
                comparable = {}
                for r in quarter_rows:
                    gap = (date.fromisoformat(current["period_end"]) - date.fromisoformat(r["asOfDate"])).days
                    if 350 <= gap <= 380:
                        comparable.setdefault(r["asOfDate"], []).append(r)
                if len(current_values) == 1 and len(comparable) == 1:
                    rows = next(iter(comparable.values()))
                    if len({r["reportedValue"]["raw"] for r in rows}) == 1:
                        prior = rows[0]
                        metric["prior_year_quarter"] = {**current, "value": prior["reportedValue"]["raw"],
                                                        "period_end": prior["asOfDate"]}
                        metric["quarter_yoy_pct"], transition = _quarter_change(
                            current, metric["prior_year_quarter"],
                            "vendor current snapshot; same currency and reported 3M duration; exact start dates/publication dates unavailable")
                        if name == "net_income":
                            metric["profit_transition"] = transition
        financials["metrics"][name] = metric
    return valuation, financials


class PublicResearchClient:
    """Explicit refresh client; construction and cache display never network.

    cache_dir must be the application's dedicated public-research cache directory.
    Fallback data is explicitly stale, with the original fetched_at preserved.
    """
    _rate_lock = threading.Lock()
    _next_request = 0.0

    def __init__(self, timeout: float = 12, max_bytes: int = 12_000_000,
                 min_interval: float = 0.15, cache_dir: Path | None = None):
        self.timeout = min(max(float(timeout), 1), 30)
        self.max_bytes = min(max(int(max_bytes), 1024), 20_000_000)
        self.min_interval = max(float(min_interval), .15)
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self._memory, self._lock = {}, threading.Lock()
        self._opener = urllib.request.build_opener(_Redirect())

    def begin_refresh(self):
        """Deduplication is scoped to one explicit refresh, not an app lifetime."""
        with self._lock:
            self._memory.clear()

    def _request(self, url: str, cancelled=None) -> tuple[dict | None, dict]:
        _public_url(url)
        _cancel(cancelled)
        key = hashlib.sha256(url.encode()).hexdigest()
        now = datetime.now(UTC).isoformat()
        provider = "SEC" if urllib.parse.urlsplit(url).hostname.endswith("sec.gov") else "Yahoo Finance"
        record = {"id": provider.split()[0].lower() + "-" + key[:12], "provider": provider,
                  "url": url, "retrieved_at": now, "fetched_at": None, "status": "missing", "stale": False,
                  "sha256": None, "bytes": 0, "error_code": None}
        with self._lock:
            memory = self._memory.get(key)
        if memory:
            return memory
        cached_path = self.cache_dir / (key + ".json") if self.cache_dir else None
        try:
            with self._rate_lock:
                delay = max(0, PublicResearchClient._next_request - time.monotonic())
                while delay > 0:
                    _cancel(cancelled)
                    time.sleep(min(delay, .1))
                    delay = max(0, PublicResearchClient._next_request - time.monotonic())
                PublicResearchClient._next_request = time.monotonic() + self.min_interval
            _cancel(cancelled)
            request = urllib.request.Request(url, headers={"User-Agent": "VerdictQuant desktop public research",
                                             "X-Research-Project": PROJECT_CONTACT, "Accept": "application/json"})
            with self._opener.open(request, timeout=self.timeout) as response:
                _public_url(response.geturl())
                raw = response.read(self.max_bytes + 1)
            _cancel(cancelled)
            if len(raw) > self.max_bytes:
                raise ValueError("PUBLIC_RESPONSE_TOO_LARGE")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("PUBLIC_RESPONSE_SCHEMA_INVALID")
            record.update(fetched_at=datetime.now(UTC).isoformat(), status="ok", bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
            if cached_path:
                try:
                    cached_path.parent.mkdir(parents=True, exist_ok=True)
                    # One lock prevents same-client atomic replacement races.
                    with self._lock:
                        temp = cached_path.with_suffix("." + str(threading.get_ident()) + ".tmp")
                        payload_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
                        temp.write_text(json.dumps({"payload": payload, "source": record, "payload_sha256": payload_hash}, ensure_ascii=True), encoding="utf-8")
                        temp.replace(cached_path)
                except OSError:
                    record["cache_warning"] = "CACHE_WRITE_FAILED"
            with self._lock:
                self._memory[key] = (payload, record)
            return payload, record
        except CancelledError:
            raise
        except urllib.error.HTTPError as exc:
            record["error_code"] = "HTTP_" + str(exc.code)
        except (urllib.error.URLError, TimeoutError, OSError):
            record["error_code"] = "PUBLIC_NETWORK_UNAVAILABLE"
        except (ValueError, TypeError):
            record["error_code"] = "PUBLIC_RESPONSE_INVALID"
        if cached_path:
            try:
                if cached_path.stat().st_size > self.max_bytes * 2:
                    raise ValueError("CACHE_TOO_LARGE")
                saved = json.loads(cached_path.read_text(encoding="utf-8"))
                previous = saved["source"]
                if previous["url"] != url or not isinstance(saved["payload"], dict):
                    raise ValueError("CACHE_INVALID")
                payload_hash = hashlib.sha256(json.dumps(saved["payload"], sort_keys=True, ensure_ascii=True).encode()).hexdigest()
                if payload_hash != saved.get("payload_sha256"):
                    raise ValueError("CACHE_CONTENT_MISMATCH")
                record.update(status="stale", stale=True, fetched_at=previous["fetched_at"],
                              sha256=previous["sha256"], bytes=previous["bytes"])
                with self._lock:
                    self._memory[key] = (saved["payload"], record)
                return saved["payload"], record
            except (OSError, ValueError, KeyError, TypeError):
                pass
        # Avoid repeating a failed global index for every stock in one refresh.
        # begin_refresh clears successes and failures before the next manual run.
        with self._lock:
            self._memory[key] = (None, record)
        return None, record

    def fetch(self, symbol: str, as_of: date | datetime, cancelled=None) -> dict:
        symbol = str(symbol).upper().strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9.-]{0,11}", symbol):
            raise ValueError("INVALID_PUBLIC_SYMBOL")
        at = _cutoff(as_of)
        result = {"symbol": symbol, "as_of": at.isoformat(), "identity": None,
                  "financials": extract_financials({}, as_of, "missing"), "filings": [],
                  "news": [], "sources": [], "errors": [],
                  "valuation": {"metrics": {}, "warnings": []}, "vendor_financials": {"metrics": {}, "warnings": []}}
        def get(url):
            payload, source = self._request(url, cancelled)
            result["sources"].append(source)
            if source["status"] != "ok":
                result["errors"].append(source["id"] + ":" + str(source["error_code"]))
            return payload, source
        tickers, ticker_source = get(TICKERS_URL)
        rows = _ticker_rows(tickers) if ticker_source["status"] == "ok" else None
        resolution = "live_sec_ticker_index"
        if rows is None:
            alternate, alternate_source = get(TICKERS_EXCHANGE_URL)
            rows = _ticker_rows(alternate) if alternate_source["status"] == "ok" else None
            if rows is not None:
                ticker_source = alternate_source
                resolution = "live_sec_exchange_index"
        matches = [row for row in (rows or []) if row.get("ticker") == symbol]
        if rows is None and symbol in IDENTITY_SEEDS:
            seed_cik, seed_name = IDENTITY_SEEDS[symbol]
            matches = [{"cik_str": seed_cik, "title": seed_name, "ticker": symbol}]
            resolution = "registered_cik_live_sec_submissions"
        if len(matches) == 1:
            try:
                cik = int(matches[0]["cik_str"])
                if not (0 < cik < 10_000_000_000):
                    raise ValueError("CIK_INVALID")
                submissions, sub_source = get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
                if not submissions or int(submissions.get("cik", 0)) != cik or symbol not in submissions.get("tickers", []) or not submissions.get("name") or _entity_key(submissions["name"]) != _entity_key(matches[0].get("title")):
                    result["errors"].append("SEC_IDENTITY_MISMATCH_OR_UNAVAILABLE")
                else:
                    result["identity"] = {"cik": cik, "name": submissions["name"], "ticker_table_name": matches[0].get("title"),
                                          "tickers": submissions["tickers"], "exchanges": submissions.get("exchanges", []),
                                          "status": "verified" if sub_source["status"] == "ok" and not sub_source["stale"] else "stale",
                                          "resolution": resolution,
                                          "source_refs": ([ticker_source["id"]] if rows is not None else []) + [sub_source["id"]]}
                    if rows is None:
                        result["identity"]["routing_seed"] = {"verified_on": "2026-09-08", "origin": TICKERS_URL,
                                                              "is_identity_evidence": False}
                    recent = submissions.get("filings", {}).get("recent", {})
                    accepted = dict(zip(recent.get("accessionNumber", []), recent.get("acceptanceDateTime", [])))
                    for i, filed in enumerate(recent.get("filingDate", [])):
                        try:
                            day = date.fromisoformat(filed)
                            form, accn = recent["form"][i], recent["accessionNumber"][i]
                            document = recent["primaryDocument"][i]
                            timestamp = accepted.get(accn)
                            if not (at.date() - timedelta(days=90) <= day <= at.date()) or form not in FORMS:
                                continue
                            if isinstance(as_of, datetime) and day == at.date() and (not timestamp or datetime.fromisoformat(timestamp.replace("Z", "+00:00")) > at):
                                continue
                            if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accn) or not document or "/" in document or "\\" in document:
                                continue
                            report_date = recent.get("reportDate", [""] * len(recent["form"]))[i]
                            result["filings"].append({"form": form, "filing_date": filed, "report_date": report_date,
                                "title": form + " · " + filed, "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn.replace('-', '')}/{urllib.parse.quote(document, safe='')}",
                                "source_refs": [sub_source["id"]], "untrusted": True})
                        except (IndexError, KeyError, TypeError, ValueError):
                            continue
                    result["filings"] = sorted(result["filings"], key=lambda r: r["filing_date"], reverse=True)[:8]
                    facts, facts_source = get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json")
                    if facts and int(facts.get("cik", 0)) == cik and facts.get("entityName") and _entity_key(facts["entityName"]) == _entity_key(submissions["name"]):
                        result["identity"]["facts_name"] = facts["entityName"]
                        result["financials"] = extract_financials(facts, as_of, facts_source["id"], accepted)
                        result["financials"]["stale"] = facts_source["stale"]
                    else:
                        result["errors"].append("SEC_FACTS_IDENTITY_MISMATCH_OR_UNAVAILABLE")
            except (ValueError, TypeError, KeyError):
                result["errors"].append("SEC_IDENTITY_SCHEMA_INVALID")
        else:
            result["errors"].append("SEC_TICKER_NOT_UNIQUELY_IDENTIFIED")
        news, news_source = get("https://query1.finance.yahoo.com/v1/finance/search?" + urllib.parse.urlencode({"q": symbol, "quotesCount": 1, "newsCount": 15}))
        for item in (news or {}).get("news", []):
            try:
                stamp = item["providerPublishTime"]
                if not _number(stamp):
                    continue
                published = datetime.fromtimestamp(stamp, UTC)
                title, url = item["title"], item["link"]
                related = item.get("relatedTickers", [])
                if not (at - timedelta(days=90) <= published <= at) or symbol not in related or not _safe_url(url) or not isinstance(title, str):
                    continue
                result["news"].append({"title": title[:500], "url": url, "published_at": published.isoformat(),
                    "publisher": str(item.get("publisher", ""))[:120], "related_symbols": related,
                    "source_refs": [news_source["id"]], "untrusted": True})
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
        unique_news = {r["url"]: r for r in result["news"]}
        result["news"] = sorted(unique_news.values(), key=lambda r: r["published_at"], reverse=True)[:8]
        if not result["news"]:
            result["errors"].append("NO_MATCHING_NEWS_WITHIN_CUTOFF")
        # Restated vendor series cannot substantiate what was known on a historical date.
        if at.date() == datetime.now(UTC).date():
            kinds = list(VALUATION_TYPES.values()) + [prefix + suffix for suffix in VENDOR_TYPES.values() for prefix in ("annual", "quarterly", "trailing")]
            period1 = int(datetime.combine(at.date() - timedelta(days=550), daytime.min, UTC).timestamp())
            period2 = int(datetime.combine(at.date() + timedelta(days=1), daytime.min, UTC).timestamp())
            series, series_source = get("https://query1.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/" + symbol + "?" + urllib.parse.urlencode({"symbol": symbol, "type": ",".join(kinds), "period1": period1, "period2": period2}))
            result["valuation"], result["vendor_financials"] = extract_timeseries(series or {}, symbol, as_of, series_source["id"])
            result["valuation"]["stale"] = result["vendor_financials"]["stale"] = series_source["stale"]
        else:
            result["valuation"]["warnings"].append("VENDOR_DISABLED_FOR_HISTORICAL_CUTOFF")
            result["vendor_financials"]["warnings"].append("VENDOR_DISABLED_FOR_HISTORICAL_CUTOFF")
        _cancel(cancelled)
        return result
