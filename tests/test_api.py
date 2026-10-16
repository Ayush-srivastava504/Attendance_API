"""HTTP tests for employees, punch in/out and regularization, run against an in-memory MongoDB double."""
import threading

import mongomock
import pytest
from fastapi.testclient import TestClient

from app import main

IST_OFFSET_MS = 19_800_000
DAY_MS = 86_400_000
JULY_6_MIDNIGHT_UTC_MS = 1_783_296_000_000


def ist_ms(day_offset, hour, minute, second=0):
    return JULY_6_MIDNIGHT_UTC_MS + day_offset * DAY_MS - IST_OFFSET_MS + ((hour * 60 + minute) * 60 + second) * 1000


@pytest.fixture()
def client(monkeypatch):
    database = mongomock.MongoClient(tz_aware=True)["test"]
    monkeypatch.setattr(main, "employees", database["employees"])
    monkeypatch.setattr(main, "logs", database["attendance_logs"])
    database["employees"].create_index("emp_code", unique=True)
    database["attendance_logs"].create_index([("emp_code", 1), ("date", 1)], unique=True)
    return TestClient(main.app)


def employee(code="EMP0001", **overrides):
    return {"emp_code": code, "name": "Asha Rao", "email": "asha@example.com", "department": "Engineering",
            "joined_on": "2026-01-05", **overrides}


def test_create_employee_and_duplicate(client):
    created = client.post("/employees", json=employee())
    assert created.status_code == 201
    assert created.json()["shift_start"] == "09:30" and isinstance(created.json()["created_at"], int)
    assert client.post("/employees", json=employee()).status_code == 409


@pytest.mark.parametrize("bad", [
    {"emp_code": "E1"}, {"shift_start": "10:00", "shift_end": "10:00"}, {"joined_on": "2026-02-30"},
    {"email": "nope"}, {"shift_start": "25:00"},
])
def test_create_employee_validation(client, bad):
    assert client.post("/employees", json=employee(**bad)).status_code == 422


def test_list_employees_paging_and_filter(client):
    for number in range(1, 6):
        client.post("/employees", json=employee(f"EMP000{number}", department="Sales" if number > 3 else "Engineering"))
    page = client.get("/employees", params={"department": "Engineering", "page": 2, "page_size": 2}).json()
    assert page["total"] == 3 and [e["emp_code"] for e in page["items"]] == ["EMP0003"]
    assert client.get("/employees", params={"page": 0}).status_code == 422
    assert client.get("/employees", params={"page_size": 101}).status_code == 422


def test_punch_in_rules(client):
    client.post("/employees", json=employee())
    body = {"emp_code": "EMP0001", "punched_at": ist_ms(0, 10, 15, 59) + 700}
    record = client.post("/attendance/punch-in", json=body).json()
    assert record["date"] == "2026-07-06" and record["late_minutes"] == 45
    assert record["punch_in"] == ist_ms(0, 10, 15, 59) and record["punch_out"] is None and record["history"] == []
    assert client.post("/attendance/punch-in", json=body).status_code == 409
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP9999"}).status_code == 404


@pytest.mark.parametrize("value", [1_783_312_500, 1.5, "1783312500000", 0, True])
def test_punch_in_rejects_bad_timestamps(client, value):
    client.post("/employees", json=employee())
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": value}).status_code == 422


def test_punch_in_rejects_absent_status(client):
    client.post("/employees", json=employee())
    assert client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "status": "ABSENT"}).status_code == 422


def test_punch_out_cycle(client):
    client.post("/employees", json=employee())
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist_ms(0, 9, 30)})
    closed = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ist_ms(0, 19, 10)})
    assert closed.status_code == 200
    assert closed.json()["work_hours"] == 9.67 and closed.json()["overtime_minutes"] == 40
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ist_ms(0, 19, 20)}).status_code == 409


def test_punch_out_errors(client):
    client.post("/employees", json=employee())
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ist_ms(0, 19, 0)}).status_code == 404
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist_ms(0, 9, 30)})
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ist_ms(0, 9, 30)}).status_code == 422
    assert client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ist_ms(1, 9, 31)}).status_code == 422


def test_overnight_shift_across_midnight(client):
    client.post("/employees", json=employee("EMP0005", shift_start="22:00", shift_end="06:00"))
    client.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ist_ms(0, 21, 55)})
    closed = client.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": ist_ms(1, 6, 40)}).json()
    assert closed["date"] == "2026-07-06" and closed["overtime_minutes"] == 40 and closed["work_hours"] == 8.75


def test_simultaneous_punch_ins_yield_one_created(client):
    client.post("/employees", json=employee())
    body = {"emp_code": "EMP0001", "punched_at": ist_ms(0, 9, 30)}
    codes = []
    threads = [threading.Thread(target=lambda: codes.append(client.post("/attendance/punch-in", json=body).status_code)) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(codes) == [201] + [409] * 7


def test_regularize_recomputes_and_appends_history(client):
    client.post("/employees", json=employee())
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist_ms(0, 10, 5)})
    client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ist_ms(0, 18, 30)})
    patch = {"punch_in": ist_ms(0, 9, 28), "reason": "biometric glitch", "regularized_by": "hr.admin", "work_hours": 99}
    record = client.patch("/attendance/EMP0001/2026-07-06", json=patch).json()
    assert record["late_minutes"] == 0 and record["work_hours"] == 9.03
    entry = record["history"][0]
    assert entry["by"] == "hr.admin" and entry["changes"]["punch_in"] == {"from": ist_ms(0, 10, 5), "to": ist_ms(0, 9, 28)}
    assert set(entry["changes"]) == {"punch_in", "late_minutes", "work_hours"}
    second = client.patch("/attendance/EMP0001/2026-07-06", json={"status": "WFH", "reason": "remote day", "regularized_by": "hr"}).json()
    assert len(second["history"]) == 2 and second["status"] == "WFH"


def test_regularize_rules(client):
    client.post("/employees", json=employee())
    client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ist_ms(0, 9, 30)})
    base = {"reason": "needs fixing", "regularized_by": "hr"}
    assert client.patch("/attendance/EMP0001/2026-07-06", json=base).status_code == 422
    assert client.patch("/attendance/EMP0001/2026-07-06", json={**base, "status": "PRESENT"}).status_code == 422
    assert client.patch("/attendance/EMP0001/2026-07-06", json={**base, "status": "LEAVE", "punch_in": ist_ms(0, 9, 0)}).status_code == 422
    assert client.patch("/attendance/EMP0001/2026-07-06", json={**base, "punch_in": ist_ms(1, 9, 0)}).status_code == 422
    assert client.patch("/attendance/EMP0001/2026-07-06", json={**base, "punch_out": ist_ms(0, 9, 0)}).status_code == 422
    assert client.patch("/attendance/EMP0001/2026-07-09", json={**base, "status": "LEAVE"}).status_code == 404
    assert client.patch("/attendance/EMP9999/2026-07-06", json={**base, "status": "LEAVE"}).status_code == 404
    leave = client.patch("/attendance/EMP0001/2026-07-06", json={**base, "status": "LEAVE"}).json()
    assert leave["punch_in"] is None and leave["late_minutes"] == 0
    assert set(client.get("/attendance/EMP0001/2026-07-06").json()["history"][0]["changes"]) == {"status", "punch_in"}


def test_list_attendance_sorting_and_total(client):
    for code in ("EMP0002", "EMP0001"):
        client.post("/employees", json=employee(code))
        for day in (0, 1):
            client.post("/attendance/punch-in", json={"emp_code": code, "punched_at": ist_ms(day, 9, 30)})
    page = client.get("/attendance", params={"page_size": 3}).json()
    assert page["total"] == 4
    assert [(r["date"], r["emp_code"]) for r in page["items"]] == [("2026-07-07", "EMP0001"), ("2026-07-07", "EMP0002"), ("2026-07-06", "EMP0001")]
    assert client.get("/attendance", params={"date_from": "2026-07-07", "date_to": "2026-07-06"}).status_code == 422
    assert client.get("/attendance", params={"date_from": "07-06-2026"}).status_code == 422
    assert client.get("/attendance", params={"status": "BOGUS"}).status_code == 422
