from datetime import datetime

from clipping.timewin import BRT, fmt_en, fmt_pt, freshness_window


def test_weekday_window_starts_yesterday_afternoon():
    w = freshness_window(datetime(2026, 11, 12, 5, 30, tzinfo=BRT), cutoff_hour=13)  # Thursday
    assert not w.monday
    assert w.start == datetime(2026, 11, 11, 13, 0, tzinfo=BRT)
    assert w.contains(datetime(2026, 11, 11, 20, 0, tzinfo=BRT))
    assert not w.contains(datetime(2026, 11, 11, 10, 0, tzinfo=BRT))


def test_monday_window_is_weekend_catch_up():
    w = freshness_window(datetime(2026, 11, 16, 5, 30, tzinfo=BRT), cutoff_hour=13)  # Monday
    assert w.monday
    assert w.start == datetime(2026, 11, 13, 13, 0, tzinfo=BRT)  # Friday afternoon
    assert w.contains(datetime(2026, 11, 14, 9, 0, tzinfo=BRT))


def test_date_formats():
    d = datetime(2026, 9, 5, 1, 0, tzinfo=BRT)
    assert fmt_pt(d) == "05/09/2026"
    assert fmt_en(d) == "Sep 05"
