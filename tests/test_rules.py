"""Unit tests for the pure business rules R1-R5 and the rounding helpers; no database needed."""
from datetime import datetime, timezone

import pytest

from app import main

IST = main.IST


def ist(year, month, day, hour, minute, second=0):
    return datetime(year, month, day, hour, minute, second, tzinfo=IST).astimezone(timezone.utc)


def late(punch_in, start="09:30", end="18:30"):
    day = main.attendance_date(punch_in, start, end)
    return main.compute_late_minutes(punch_in, day, start, end)


@pytest.mark.parametrize(
    "hour, minute, second, expected",
    [(9, 40, 0, 0), (9, 40, 1, 10), (10, 15, 59, 45), (9, 0, 0, 0), (9, 30, 0, 0)],
)
def test_late_follows_grace_rule(hour, minute, second, expected):
    assert late(ist(2026, 7, 6, hour, minute, second)) == expected


def test_overnight_punch_in_after_midnight_belongs_to_previous_day():
    punch = ist(2026, 7, 7, 1, 30)
    assert main.attendance_date(punch, "22:00", "06:00") == "2026-07-06"
    assert main.attendance_date(ist(2026, 7, 7, 6, 0), "22:00", "06:00") == "2026-07-07"


def test_overnight_late_and_overtime():
    punch_in, punch_out = ist(2026, 7, 6, 21, 55), ist(2026, 7, 7, 6, 40)
    assert late(punch_in, "22:00", "06:00") == 0
    assert main.compute_overtime_minutes(punch_out, "2026-07-06", "22:00", "06:00") == 40


@pytest.mark.parametrize("minutes, expected", [(29, 0), (30, 30), (40, 40)])
def test_overtime_minimum(minutes, expected):
    punch_out = ist(2026, 7, 6, 18, 30) + (main.timedelta(minutes=minutes))
    assert main.compute_overtime_minutes(punch_out, "2026-07-06", "09:30", "18:30") == expected


@pytest.mark.parametrize("seconds, expected", [(3600, 100), (17, 0), (18, 1), (54, 2), (32760, 910), (12600, 350)])
def test_work_hundredths_round_half_up(seconds, expected):
    start = ist(2026, 7, 6, 9, 0)
    assert main.compute_work_hundredths(start, start + main.timedelta(seconds=seconds)) == expected


def test_half_day_uses_rounded_hours():
    employee = {"shift_start": "09:30", "shift_end": "18:30"}
    start = ist(2026, 7, 6, 9, 30)
    just_under = main.derived_fields("PRESENT", start, start + main.timedelta(seconds=16199), employee, "2026-07-06")
    exactly = main.derived_fields("PRESENT", start, start + main.timedelta(seconds=16200), employee, "2026-07-06")
    assert just_under["work_hours"] == 4.5 and just_under["half_day"] is False
    assert exactly["half_day"] is False
    below = main.derived_fields("PRESENT", start, start + main.timedelta(seconds=16000), employee, "2026-07-06")
    assert below["work_hours"] == 4.44 and below["half_day"] is True


def test_off_statuses_have_no_derived_values():
    fields = main.derived_fields("LEAVE", None, None, {"shift_start": "09:30", "shift_end": "18:30"}, "2026-07-06")
    assert fields == {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}


def test_half_up_is_not_bankers_rounding():
    assert main.half_up(0.125, 2) == 0.13
    assert main.half_up(2.5, 0) == 3.0
    assert main.half_up(37.5, 1) == 37.5


def test_epoch_conversion_truncates_to_seconds():
    value = main.ms_to_datetime(1_783_312_500_900)
    assert main.datetime_to_ms(value) == 1_783_312_500_000


@pytest.mark.parametrize(
    "first, last, expected",
    [("2026-07-01", "2026-07-31", 23), ("2026-07-13", "2026-07-31", 15), ("2026-08-01", "2026-07-31", 0)],
)
def test_working_days(first, last, expected):
    assert main.working_days_between(main.Date.fromisoformat(first), main.Date.fromisoformat(last)) == expected
