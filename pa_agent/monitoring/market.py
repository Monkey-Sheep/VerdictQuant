"""Bounded public HTTP and read-only NAV cache; explicit session-qualified data."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import statistics
import urllib.parse
import urllib.request
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .calendar import CalendarError, Calendars


def indicators(rows: list[dict]) -> dict:
    rows = sorted(rows, key=lambda row: row["date"])
    if not rows or len({row["date"] for row in rows}) != len(rows):
        raise ValueError("EMPTY_OR_DUPLICATE_DATES")
    for row in rows:
        date.fromisoformat(row["date"])
        if any(isinstance(row[key], bool) or not isinstance(row[key], (int, float))
               or not math.isfinite(row[key]) or row[key] <= 0 for key in ("close", "high")):
            raise ValueError("INVALID_PRICE")
        if row["high"] + 1e-8 < row["close"]:
            raise ValueError("HIGH_BELOW_CLOSE")
    values = [row["close"] for row in rows]
    n = len(values)
    means = {str(period): statistics.mean(values[-period:]) if n >= period else None
             for period in (20, 50, 200)}
    last = values[-1]
    since = (date.fromisoformat(rows[-1]["date"]) - timedelta(weeks=52)).isoformat()
    high = max(row["high"] for row in rows if row["date"] >= since)
    return {
        "date": rows[-1]["date"], "samples": n, "close": last,
        "daily_pct": (last / values[-2] - 1) * 100 if n >= 2 else None,
        "five_session_pct": (last / values[-6] - 1) * 100 if n >= 6 else None,
        "rolling_52week_high": high, "drawdown_52week_pct": (last / high - 1) * 100,
        "full_52week_coverage": rows[0]["date"] <= since,
        "ma": means,
        "two_completed_closes_below_own_ma200": (
            values[-1] < statistics.mean(values[-200:])
            and values[-2] < statistics.mean(values[-201:-1]) if n >= 201 else None
        ),
        "ma200_twenty_session_slope_pct": (
            (means["200"] / statistics.mean(values[-220:-20]) - 1) * 100 if n >= 220 else None
        ),
        "recent_10": rows[-10:],
    }


class PublicClient:
    def __init__(self, policy: dict, calendars: Calendars, fund_db: Path | None = None):
        self.policy, self.calendars, self.fund_db = policy, calendars, fund_db

    def request(self, url: str, body: dict | None = None) -> tuple[dict, dict]:
        allowed = {"query1.finance.yahoo.com", "query2.finance.yahoo.com", "api.efunds.com.cn"}
        if urllib.parse.urlsplit(url).hostname not in allowed:
            raise ValueError("PUBLIC_SOURCE_NOT_ALLOWED")
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                         headers=headers)
        limit = self.policy["max_response_bytes"]
        with urllib.request.urlopen(request, timeout=self.policy["http_timeout_seconds"]) as response:
            if urllib.parse.urlsplit(response.geturl()).hostname not in allowed:
                raise ValueError("PUBLIC_REDIRECT_NOT_ALLOWED")
            raw = response.read(limit + 1)
        if len(raw) > limit:
            raise ValueError("PUBLIC_RESPONSE_TOO_LARGE")
        return json.loads(raw), {"url": url, "retrieved_at": datetime.now(UTC).isoformat(),
                                 "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}

    def equity(self, symbol: str, market: str, at: datetime) -> dict:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(symbol, safe="") + "?range=2y&interval=1d"
        payload, source = self.request(url)
        chart = payload["chart"]["result"][0]
        meta = chart["meta"]
        if meta.get("symbol") != symbol:
            raise ValueError("PUBLIC_SYMBOL_MISMATCH")
        zone = ZoneInfo(meta["exchangeTimezoneName"])
        errors = []
        try:
            cutoff = self.calendars.latest_completed(at, market)
        except CalendarError as exc:
            cutoff = at.astimezone(zone).date() - timedelta(days=1)
            errors.append(str(exc))
        quote = chart["indicators"]["quote"][0]
        rows = []
        for i, stamp in enumerate(chart["timestamp"]):
            day = datetime.fromtimestamp(stamp, zone).date()
            close = quote["close"][i]
            if day <= cutoff and close is not None:
                # FX is consumed as a closing conversion rate, not an OHLC asset.
                # Some provider FX highs use a different fixing; do not mix them.
                high = close if market == "FX" else quote["high"][i] or close
                rows.append({"date": day.isoformat(), "close": close, "high": high})
        metrics = indicators(rows)
        if metrics["date"] != cutoff.isoformat():
            errors.append("STALE_COMPLETED_HISTORY")
        minimum = 220 if symbol in self.policy["core_review"] else 50 if market == "CN" else 20
        if metrics["samples"] < minimum:
            errors.append("INSUFFICIENT_HISTORY")
        stamp = meta.get("regularMarketTime")
        price = meta.get("regularMarketPrice")
        live = None
        if isinstance(stamp, (int, float)) and isinstance(price, (int, float)) and not isinstance(price, bool) and math.isfinite(price) and price > 0:
            quoted_at = datetime.fromtimestamp(stamp, UTC)
            live = {"price": price, "quoted_at": quoted_at.isoformat(),
                    "fresh": -30 <= (at - quoted_at).total_seconds() <= self.policy["quote_max_age_seconds"]}
        return {**metrics, "symbol": symbol, "market": market, "currency": meta.get("currency"),
                "expected_completed_date": cutoff.isoformat(), "qualified": not errors,
                "errors": errors, "source": source, "provider_timezone": meta["exchangeTimezoneName"],
                "quote": live, "basis": "FX daily closing rate; extremes based on closes" if market == "FX" else "provider quote OHLC; not dividend-reinvested total return",
                "rows": rows}

    def fund(self, at: datetime) -> dict:
        code = self.policy["fund"]
        cutoff = self.calendars.latest_completed(at, "CN")
        body = {"fundCode": code, "pageIndex": 0, "pageSize": 31,
                "startDate": (cutoff - timedelta(days=31)).isoformat(), "endDate": cutoff.isoformat(), "siteID": "1"}
        payload, source = self.request("https://api.efunds.com.cn/xcowch/front/fund/nav", body)
        if payload.get("status") != 1 or not payload.get("data", {}).get("data"):
            raise ValueError("OFFICIAL_NAV_UNAVAILABLE")
        official = payload["data"]["data"]
        latest = max(row["navDate"] for row in official)
        if latest > cutoff.isoformat():
            raise ValueError("UNCOMPLETED_OFFICIAL_NAV_DATE")
        records, errors = {}, []
        if self.fund_db and self.fund_db.is_file():
            with closing(sqlite3.connect(self.fund_db.resolve().as_uri() + "?mode=ro", uri=True)) as con:
                records = {day: float(nav) for day, nav in con.execute(
                    "SELECT date,nav FROM fund_nav_records WHERE code=? AND date<=? ORDER BY date DESC LIMIT 550", (code, latest))}
        else:
            errors.append("PUBLIC_NAV_HISTORY_CACHE_MISSING")
        for row in official:
            records[row["navDate"]] = float(row["netValue"])
        metrics = indicators([{"date": day, "close": nav, "high": nav} for day, nav in records.items()])
        if metrics["samples"] < 220:
            errors.append("INSUFFICIENT_NAV_HISTORY")
        publication = "PUBLISHED"
        if latest != cutoff.isoformat():
            local = at.astimezone(ZoneInfo("Asia/Shanghai"))
            previous = self.calendars.latest_completed(self.calendars.close_at(cutoff, "CN") - timedelta(minutes=3), "CN")
            publication = "PENDING_PUBLICATION" if cutoff == local.date() and latest == previous.isoformat() else "STALE"
            errors.append(publication)
        return {**metrics, "symbol": code, "market": "CN", "currency": "CNY", "qualified": not errors,
                "errors": errors, "expected_completed_date": cutoff.isoformat(), "publication": publication,
                "source": {**source, "request": body}, "basis": "official recent unit NAV plus read-only public history cache",
                "rows": [{"date": day, "close": nav, "high": nav} for day, nav in sorted(records.items())]}

    def collect(self, at: datetime, cancelled=None) -> dict:
        result = {"observed_at": at.isoformat(), "assets": {}, "errors": {},
                  "source_policy": "public-data-only", "real_account_access": False, "orders": False}
        mapping = {symbol: "US" for symbol in self.policy["us_universe"]}
        mapping.update(self.policy["auxiliary"])
        def invoke(function, *args):
            if cancelled is not None and cancelled.is_set():
                raise CancelledError()
            return function(*args)
        with ThreadPoolExecutor(max_workers=self.policy["max_workers"]) as pool:
            futures = {pool.submit(invoke, self.equity, symbol, market, at): symbol for symbol, market in mapping.items()}
            futures[pool.submit(invoke, self.fund, at)] = self.policy["fund"]
            for future in as_completed(futures):
                if cancelled is not None and cancelled.is_set():
                    for pending in futures:
                        pending.cancel()
                    raise CancelledError()
                symbol = futures[future]
                try:
                    asset = future.result()
                    result["assets"][symbol] = asset
                    if asset["errors"]:
                        result["errors"][symbol] = asset["errors"]
                except Exception as exc:
                    known = {"EMPTY_OR_DUPLICATE_DATES", "INVALID_PRICE", "HIGH_BELOW_CLOSE", "PUBLIC_SYMBOL_MISMATCH", "OFFICIAL_NAV_UNAVAILABLE", "UNCOMPLETED_OFFICIAL_NAV_DATE"}
                    result["errors"][symbol] = [str(exc) if str(exc) in known else "PUBLIC_FETCH_FAILED:" + type(exc).__name__]
        return result


def core_review_checks(data: dict, policy: dict) -> dict:
    results = {}
    for symbol, rule in policy["core_review"].items():
        asset = data["assets"].get(symbol)
        if not asset or not asset.get("qualified"):
            results[symbol] = {"status": "DATA_UNQUALIFIED", "manual_review_required": None}
            continue
        below = asset["two_completed_closes_below_own_ma200"]
        drawdown = -asset["drawdown_52week_pct"] >= rule["drawdown_pct"] and asset.get("full_52week_coverage", False)
        if rule["requires_below_ma50"]:
            drawdown = drawdown and asset["ma"]["50"] is not None and asset["close"] < asset["ma"]["50"]
        results[symbol] = {"status": "PRICE_GATE_REACHED" if below or drawdown else "PRICE_GATE_NOT_REACHED",
                           "two_closes_below_ma200": below, "drawdown_gate_reached": drawdown,
                           "manual_review_required": True if below else None if drawdown else False,
                           "corroboration_required": bool(drawdown and not below),
                           "first_trim_max_shares": None, "order_authorized": False}
    return results
