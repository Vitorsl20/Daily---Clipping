"""Freshness window: today + yesterday from the afternoon; Monday = weekend catch-up from Friday afternoon."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

BRT = ZoneInfo("America/Sao_Paulo")
UTC = ZoneInfo("UTC")

# Local time zones per country, used when an article page prints a naive time.
COUNTRY_TZ = {
    "BR": ZoneInfo("America/Sao_Paulo"),
    "AR": ZoneInfo("America/Argentina/Buenos_Aires"),
    "MX": ZoneInfo("America/Mexico_City"),
    "CO": ZoneInfo("America/Bogota"),
    "PE": ZoneInfo("America/Lima"),
    "CL": ZoneInfo("America/Santiago"),
    "UY": ZoneInfo("America/Montevideo"),
    "PA": ZoneInfo("America/Panama"),
}

MONTHS_EN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


@dataclass(frozen=True)
class Window:
    start: datetime   # aware, BRT
    end: datetime     # aware, BRT (run time)
    monday: bool

    def contains(self, dt: datetime | None, future_tolerance: timedelta = timedelta(hours=2)) -> bool:
        if dt is None:
            return False
        return self.start <= dt <= self.end + future_tolerance

    def contains_date(self, d: date) -> bool | None:
        """True/False when a bare calendar date settles it; None when it falls on the start day (ambiguous)."""
        if d > self.end.date():
            return False
        if d < self.start.date():
            return False
        if d == self.start.date():
            return None
        return True

    def describe(self) -> str:
        fmt = "%d/%m %H:%M"
        label = " (segunda-feira: janela de fim de semana desde sexta à tarde)" if self.monday else ""
        return f"de {self.start.strftime(fmt)} a {self.end.strftime(fmt)} BRT{label}"


def freshness_window(now: datetime | None = None, cutoff_hour: int | None = None) -> Window:
    if cutoff_hour is None:
        cutoff_hour = int(os.environ.get("FRESHNESS_CUTOFF_HOUR", "13"))
    now = (now or datetime.now(UTC)).astimezone(BRT)
    monday = now.weekday() == 0
    days_back = 3 if monday else 1
    start_day = now.date() - timedelta(days=days_back)
    start = datetime.combine(start_day, time(cutoff_hour, 0), BRT)
    return Window(start=start, end=now, monday=monday)


def fmt_pt(dt: datetime) -> str:
    return dt.astimezone(BRT).strftime("%d/%m/%Y")


def fmt_en(dt: datetime) -> str:
    d = dt.astimezone(BRT)
    return f"{MONTHS_EN[d.month - 1]} {d.day:02d}"
