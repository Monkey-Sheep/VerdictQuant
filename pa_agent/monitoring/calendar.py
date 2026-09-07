"""Explicit exchange sessions and IANA zones; unavailable coverage fails closed."""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


class CalendarError(ValueError):
    pass


class Calendars:
    def __init__(self, path: Path | None = None):
        self.path = path or Path(__file__).with_name("calendars.json")
        self.data = json.loads(self.path.read_text(encoding="utf-8"))["markets"]

    def is_session(self, day: date, market: str) -> bool:
        item = self.data[market]
        if item["verified"] is not True:
            raise CalendarError("CALENDAR_UNVERIFIED:" + market)
        if not item["coverage_start"] <= day.isoformat() <= item["coverage_end"]:
            raise CalendarError("CALENDAR_OUTSIDE_COVERAGE:" + market)
        return day.weekday() < 5 and day.isoformat() not in item["holidays"] and not any(
            start <= day.isoformat() <= end for start, end in item["holiday_ranges"]
        )

    def close_at(self, day: date, market: str) -> datetime:
        item = self.data[market]
        clock = item["special_closes"].get(day.isoformat(), item["close"])
        return datetime.combine(day, time.fromisoformat(clock), ZoneInfo(item["timezone"]))

    def latest_completed(self, at: datetime, market: str) -> date:
        if at.tzinfo is None:
            raise CalendarError("TIMEZONE_REQUIRED")
        if market == "CRYPTO":
            return at.astimezone(UTC).date() - timedelta(days=1)
        if market == "FX":
            # Provider daily session, not a US-exchange calendar or live FX quote.
            day = at.astimezone(ZoneInfo("Europe/London")).date() - timedelta(days=1)
            while day.weekday() >= 5:
                day -= timedelta(days=1)
            return day
        local = at.astimezone(ZoneInfo(self.data[market]["timezone"]))
        day = local.date()
        for _ in range(20):
            if self.is_session(day, market) and at >= self.close_at(day, market) + timedelta(minutes=2):
                return day
            day -= timedelta(days=1)
        raise CalendarError("NO_COMPLETED_SESSION:" + market)
