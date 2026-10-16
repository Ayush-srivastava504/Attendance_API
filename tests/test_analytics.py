"""Analytics and explain tests with hand-computed expectations; they need a real MongoDB 6.0+ (set TEST_MONGO_URI)."""
import json
import os
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

from app import main

pytestmark = pytest.mark.skipif(not os.getenv("TEST_MONGO_URI"), reason="TEST_MONGO_URI not set")


def staff(code, department, joined, **shift):
    return {"emp_code": code, "name": f"Name {code}", "email": f"{code}@example.com", "department": department,
            "shift_start": "09:30", "shift_end": "18:30", "joined_on": joined,
            "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc), **shift}


def log(code, day, status="PRESENT", late=0, hours=None, half=False, overtime=0):
    punched = status in ("PRESENT", "WFH", "ON_DUTY")
    return {"emp_code": code, "date": day, "status": status,
            "punch_in": datetime.fromisoformat(f"{day}T04:00:00+00:00") if punched else None,
            "punch_out": datetime.fromisoformat(f"{day}T13:00:00+00:00") if hours else None,
            "work_hours": hours, "late_minutes": late, "overtime_minutes": overtime, "half_day": half, "history": []}


@pytest.fixture(scope="module")
def client():
    database = MongoClient(os.environ["TEST_MONGO_URI"], tz_aware=True)["attendance_analytics_test"]
    database.client.drop_database(database.name)
    main.db, main.employees, main.logs = database, database["employees"], database["attendance_logs"]
    main.ensure_indexes()
    database["employees"].insert_many([
        staff("EMP0001", "Engineering", "2026-01-05"), staff("EMP0002", "Engineering", "2026-07-13"),
        staff("EMP0007", "Engineering", "2026-01-01"), staff("EMP0003", "Sales", "2026-02-01"),
        staff("EMP0004", "Sales", "2026-02-01"), staff("EMP0005", "Support", "2026-03-10"),
    ])
    database["attendance_logs"].insert_many([
        log("EMP0001", "2026-07-06", late=30, hours=9.0), log("EMP0001", "2026-07-07", hours=3.5, half=True),
        log("EMP0001", "2026-07-11", hours=8.0), log("EMP0002", "2026-07-14", late=10, hours=8.98),
        log("EMP0002", "2026-07-15", status="ON_DUTY", hours=8.0), log("EMP0003", "2026-07-06", late=30, hours=9.0),
        log("EMP0003", "2026-07-08", status="LEAVE"), log("EMP0004", "2026-07-07", status="WFH", late=20, hours=3.5, half=True),
        log("EMP0099", "2026-07-06", late=500, hours=9.0),
    ])
    with TestClient(main.app) as test_client:
        yield test_client


def test_employee_monthly_counts_from_join_date(client):
    first = client.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"}).json()
    assert first["working_days"] == 23 and first["present_days"] == 1.5 and first["late_count"] == 1
    assert first["total_late_minutes"] == 30 and first["attendance_pct"] == 6.52
    joiner = client.get("/analytics/employees/EMP0002/monthly", params={"month": "2026-07"}).json()
    assert joiner["working_days"] == 15 and joiner["present_days"] == 2 and joiner["attendance_pct"] == 13.33
    assert client.get("/analytics/employees/EMP0002/monthly", params={"month": "2026-06"}).json()["attendance_pct"] is None
    assert client.get("/analytics/employees/EMP4040/monthly", params={"month": "2026-07"}).status_code == 404


def test_department_summary_counts_people_without_logs(client):
    engineering = client.get("/analytics/departments/summary", params={"month": "2026-07", "department": "Engineering"}).json()["items"][0]
    assert engineering["headcount"] == 3 and engineering["present_days"] == 3.5 and engineering["avg_work_hours"] == 7.5
    assert engineering["late_count"] == 2 and engineering["total_late_minutes"] == 40 and engineering["on_duty_count"] == 1
    june = client.get("/analytics/departments/summary", params={"month": "2026-06"}).json()["items"]
    assert {row["department"]: row["headcount"] for row in june} == {"Engineering": 2, "Sales": 2, "Support": 1}
    assert [row["avg_work_hours"] for row in june] == [None, None, None]
    assert client.get("/analytics/departments/summary", params={"month": "2026-01"}).json()["items"][0]["headcount"] == 2


def test_leaderboard_ties_share_rank_and_limit_follows_rank(client):
    def board(limit):
        items = client.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": limit}).json()["items"]
        return [(row["rank"], row["emp_code"]) for row in items]
    assert board(1) == [(1, "EMP0001"), (1, "EMP0003")]
    assert board(2) == [(1, "EMP0001"), (1, "EMP0003")]
    assert board(3) == [(1, "EMP0001"), (1, "EMP0003"), (3, "EMP0004")]
    assert board(4)[-1] == (4, "EMP0002")


def test_trend_has_one_row_per_day_and_short_first_window(client):
    body = client.get("/analytics/departments/Engineering/trend", params={"from": "2026-07-04", "to": "2026-07-10"}).json()
    rows = body["items"]
    assert [row["date"] for row in rows] == [f"2026-07-{day:02d}" for day in range(4, 11)]
    assert [row["attendance_rate"] for row in rows] == [None, None, 0.5, 0.25, 0.0, 0.0, 0.0]
    assert [row["moving_avg_7d"] for row in rows] == [None, None, 0.5, 0.375, 0.25, 0.1875, 0.15]
    assert [row["is_working_day"] for row in rows] == [False, False, True, True, True, True, True]
    assert all(row["headcount"] == 2 for row in rows) and rows[2]["late_count"] == 1
    quiet = client.get("/analytics/departments/Support/trend", params={"from": "2026-07-01", "to": "2026-07-03"}).json()["items"]
    assert len(quiet) == 3 and [row["attendance_rate"] for row in quiet] == [0.0, 0.0, 0.0]
    assert client.get("/analytics/departments/Nowhere/trend", params={"from": "2026-07-01", "to": "2026-07-03"}).status_code == 404
    assert client.get("/analytics/departments/Sales/trend", params={"from": "2026-01-01", "to": "2026-06-30"}).status_code == 422


@pytest.mark.parametrize("endpoint, params", [
    ("attendance_list", {}),
    ("attendance_list", {"emp_code": "EMP0001", "date_from": "2026-07-01", "date_to": "2026-07-31"}),
    ("attendance_list", {"status": "LEAVE", "date_from": "2026-07-01"}),
    ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
    ("department_summary", {"month": "2026-07"}),
    ("late_leaderboard", {"month": "2026-07", "department": "Sales"}),
    ("department_trend", {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-31"}),
])
def test_explain_plans_use_indexes(client, endpoint, params):
    plan = json.dumps(client.get(f"/admin/explain/{endpoint}", params=params).json()["explain"])
    assert "IXSCAN" in plan and "COLLSCAN" not in plan


def test_explain_requires_parameters(client):
    assert client.get("/admin/explain/employee_monthly").status_code == 422
