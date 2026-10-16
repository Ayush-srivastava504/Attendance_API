"""Employee Attendance & Analytics API: FastAPI over MongoDB with punch in/out, regularization, aggregation analytics and explain."""
import json
import os
import re
from calendar import monthrange
from contextlib import asynccontextmanager
from datetime import date as Date
from datetime import datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from time import perf_counter
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt, StringConstraints, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

GRACE_SECONDS = 10 * 60
MIN_OVERTIME_MINUTES = 30
HALF_DAY_BELOW_HUNDREDTHS = 450
MAX_SHIFT_LENGTH = timedelta(hours=24)
MAX_TREND_DAYS = 92
EPOCH_MS_MIN = 100_000_000_000
EPOCH_MS_MAX = 4_102_444_800_000

PRESENCE_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]
OFF_STATUSES = ["ABSENT", "LEAVE"]
Status = Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
EpochMs = Annotated[StrictInt, Field(ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]

DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
MONTH_PATTERN = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=UTC,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db["employees"]
logs = db["attendance_logs"]

indexes_ready = False


def ensure_indexes():
    global indexes_ready
    employees.create_index([("emp_code", ASCENDING)], unique=True, name="emp_code_unique")
    employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="department_emp_code")
    employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined_on")
    employees.create_index([("joined_on", ASCENDING)], name="joined_on")
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="emp_code_date_unique")
    logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp_code")
    logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="status_date_emp_code")
    logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_code_punch_in")
    indexes_ready = True


@asynccontextmanager
async def lifespan(_: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError:
        pass
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.middleware("http")
async def add_timing_header(request: Request, call_next):
    started = perf_counter()
    response = await call_next(request)
    response.headers["X-Process-Time-Ms"] = f"{(perf_counter() - started) * 1000:.1f}"
    return response


@app.exception_handler(PyMongoError)
async def database_unavailable(_: Request, error: PyMongoError):
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


def unprocessable(message: str) -> HTTPException:
    return HTTPException(status_code=422, detail=message)


def not_found(message: str) -> HTTPException:
    return HTTPException(status_code=404, detail=message)


def conflict(message: str) -> HTTPException:
    return HTTPException(status_code=409, detail=message)


def aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def whole_seconds(value: datetime) -> datetime:
    return aware(value).replace(microsecond=0)


def now_utc() -> datetime:
    return whole_seconds(datetime.now(UTC))


def ms_to_datetime(ms: int) -> datetime:
    return EPOCH + timedelta(seconds=ms // 1000)


def datetime_to_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    return (aware(value) - EPOCH) // timedelta(milliseconds=1)


def half_up(value, places: int = 2) -> float:
    exact = Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
    return float(exact.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def parse_clock(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return parse_clock(shift_end) <= parse_clock(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = aware(punch_in).astimezone(IST)
    day = local.date()
    if is_overnight(shift_start, shift_end) and local.time() < parse_clock(shift_end):
        day -= timedelta(days=1)
    return day.isoformat()


def shift_window(day: str, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    base = Date.fromisoformat(day)
    start = datetime.combine(base, parse_clock(shift_start), IST)
    end = datetime.combine(base, parse_clock(shift_end), IST)
    if is_overnight(shift_start, shift_end):
        end += timedelta(days=1)
    return start, end


def compute_late_minutes(punch_in: datetime, day: str, shift_start: str, shift_end: str) -> int:
    start, _ = shift_window(day, shift_start, shift_end)
    seconds = int((punch_in - start).total_seconds())
    return seconds // 60 if seconds > GRACE_SECONDS else 0


def compute_overtime_minutes(punch_out: datetime, day: str, shift_start: str, shift_end: str) -> int:
    _, end = shift_window(day, shift_start, shift_end)
    minutes = int((punch_out - end).total_seconds()) // 60
    return minutes if minutes >= MIN_OVERTIME_MINUTES else 0


def compute_work_hundredths(punch_in: datetime, punch_out: datetime) -> int:
    seconds = int((punch_out - punch_in).total_seconds())
    return (2 * seconds + 36) // 72


def derived_fields(status: str, punch_in: Optional[datetime], punch_out: Optional[datetime], employee: dict, day: str) -> dict:
    empty = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENCE_STATUSES or punch_in is None:
        return empty
    shift_start, shift_end = employee["shift_start"], employee["shift_end"]
    late = compute_late_minutes(punch_in, day, shift_start, shift_end)
    if punch_out is None:
        return {**empty, "late_minutes": late}
    hundredths = compute_work_hundredths(punch_in, punch_out)
    return {
        "work_hours": hundredths / 100,
        "late_minutes": late,
        "overtime_minutes": compute_overtime_minutes(punch_out, day, shift_start, shift_end),
        "half_day": hundredths < HALF_DAY_BELOW_HUNDREDTHS,
    }


def working_days_between(first: Date, last: Date) -> int:
    days = (last - first).days + 1
    if days <= 0:
        return 0
    full_weeks, extra = divmod(days, 7)
    leftover = sum(1 for offset in range(extra) if (first + timedelta(days=offset)).weekday() < 5)
    return full_weeks * 5 + leftover


def valid_date(value: str, name: str) -> Date:
    if not DATE_PATTERN.fullmatch(value):
        raise unprocessable(f"{name} must be YYYY-MM-DD")
    try:
        return Date.fromisoformat(value)
    except ValueError:
        raise unprocessable(f"{name} is not a real calendar date")


def month_bounds(month: str) -> tuple[Date, Date]:
    if not MONTH_PATTERN.fullmatch(month):
        raise unprocessable("month must be YYYY-MM")
    year, number = int(month[:4]), int(month[5:])
    try:
        return Date(year, number, 1), Date(year, number, monthrange(year, number)[1])
    except ValueError:
        raise unprocessable("month is out of range")


def trend_window(date_from: str, date_to: str) -> tuple[Date, Date]:
    first, last = valid_date(date_from, "from"), valid_date(date_to, "to")
    if last < first:
        raise unprocessable("to must not be before from")
    if (last - first).days + 1 > MAX_TREND_DAYS:
        raise unprocessable(f"range must not exceed {MAX_TREND_DAYS} days")
    return first, last


def require(value: Optional[str], name: str) -> str:
    if not value:
        raise unprocessable(f"{name} is required")
    return value


def require_employee(emp_code: str) -> dict:
    employee = employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise not_found("employee not found")
    return employee


def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": datetime_to_ms(doc["created_at"]),
    }


def history_out(entry: dict) -> dict:
    changes = {}
    for field, pair in entry["changes"].items():
        if field in ("punch_in", "punch_out"):
            pair = {key: datetime_to_ms(value) for key, value in pair.items()}
        changes[field] = pair
    return {"at": datetime_to_ms(entry["at"]), "by": entry["by"], "reason": entry["reason"], "changes": changes}


def attendance_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_ms(doc.get("punch_in")),
        "punch_out": datetime_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day")),
        "history": [history_out(entry) for entry in doc.get("history") or []],
    }


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: Annotated[str, StringConstraints(min_length=1, max_length=50)]
    shift_start: str = Field(default="09:30", pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    shift_end: str = Field(default="18:30", pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    joined_on: str = Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

    @model_validator(mode="after")
    def check_shift_and_join_date(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        try:
            Date.fromisoformat(self.joined_on)
        except ValueError:
            raise ValueError("joined_on is not a real calendar date")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


@app.get("/health")
def health():
    client.admin.command("ping")
    if not indexes_ready:
        ensure_indexes()
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        employees.insert_one(doc)
    except DuplicateKeyError:
        raise conflict("emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    query = {"department": department} if department else {}
    cursor = employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {
        "items": [employee_out(doc) for doc in cursor],
        "total": employees.count_documents(query),
        "page": page,
        "page_size": page_size,
    }


@app.get("/employees/{emp_code}")
def get_employee(emp_code: str):
    return employee_out(require_employee(emp_code))


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = require_employee(body.emp_code)
    punched_at = ms_to_datetime(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_date(punched_at, employee["shift_start"], employee["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        **derived_fields(body.status, punched_at, None, employee, day),
        "history": [],
    }
    try:
        logs.insert_one(doc)
    except DuplicateKeyError:
        raise conflict("already punched in for this date")
    return attendance_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = require_employee(body.emp_code)
    punched_at = ms_to_datetime(body.punched_at) if body.punched_at is not None else now_utc()
    record = logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}},
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise not_found("no punch-in found for this employee")
    if record.get("punch_out") is not None:
        raise conflict("already punched out")
    started = whole_seconds(record["punch_in"])
    if punched_at <= started or punched_at - started > MAX_SHIFT_LENGTH:
        raise unprocessable("punched_at must be after punch-in and within 24 hours of it")
    fields = derived_fields(record["status"], started, punched_at, employee, record["date"])
    updated = logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": punched_at, **fields}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise conflict("already punched out")
    return attendance_out(updated)


def attendance_query(emp_code: Optional[str], date_from: Optional[str], date_to: Optional[str], status: Optional[str]) -> dict:
    query: dict[str, Any] = {}
    if emp_code:
        query["emp_code"] = emp_code
    start = valid_date(date_from, "date_from") if date_from else None
    end = valid_date(date_to, "date_to") if date_to else None
    if start and end and start > end:
        raise unprocessable("date_from must not be after date_to")
    if start or end:
        query["date"] = {}
        if start:
            query["date"]["$gte"] = start.isoformat()
        if end:
            query["date"]["$lte"] = end.isoformat()
    if status:
        query["status"] = status
    return query


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    query = attendance_query(emp_code, date_from, date_to, status)
    cursor = logs.find(query, {"_id": 0}).sort(ATTENDANCE_SORT).skip((page - 1) * page_size).limit(page_size)
    return {
        "items": [attendance_out(doc) for doc in cursor],
        "total": logs.count_documents(query),
        "page": page,
        "page_size": page_size,
    }


@app.get("/attendance/{emp_code}/{date}")
def get_attendance(emp_code: str, date: str):
    day = valid_date(date, "date").isoformat()
    record = logs.find_one({"emp_code": emp_code, "date": day})
    if record is None:
        raise not_found("no record for this employee and date")
    return attendance_out(record)


def current_state(record: dict) -> dict:
    punch_in, punch_out = record.get("punch_in"), record.get("punch_out")
    return {
        "status": record["status"],
        "punch_in": whole_seconds(punch_in) if punch_in else None,
        "punch_out": whole_seconds(punch_out) if punch_out else None,
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes") or 0,
        "overtime_minutes": record.get("overtime_minutes") or 0,
        "half_day": bool(record.get("half_day")),
    }


def corrected_state(current: dict, body: RegularizeIn, employee: dict, day: str) -> dict:
    status = body.status or current["status"]
    if status in OFF_STATUSES:
        if body.punch_in is not None or body.punch_out is not None:
            raise unprocessable(f"{status} records cannot carry punch times")
        punch_in = punch_out = None
    else:
        punch_in = ms_to_datetime(body.punch_in) if body.punch_in is not None else current["punch_in"]
        punch_out = ms_to_datetime(body.punch_out) if body.punch_out is not None else current["punch_out"]
        if punch_in is None:
            raise unprocessable("a presence status requires a punch_in")
        if body.punch_in is not None and attendance_date(punch_in, employee["shift_start"], employee["shift_end"]) != day:
            raise unprocessable("punch_in must stay on the record's attendance date")
        if punch_out is not None and not punch_in < punch_out <= punch_in + MAX_SHIFT_LENGTH:
            raise unprocessable("punch_out must be after punch_in and within 24 hours of it")
    return {"status": status, "punch_in": punch_in, "punch_out": punch_out, **derived_fields(status, punch_in, punch_out, employee, day)}


def concurrency_guard(record: dict) -> dict:
    history = record.get("history")
    return {
        "_id": record["_id"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "history": {"$size": len(history)} if history is not None else {"$exists": False},
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    day = valid_date(date, "date").isoformat()
    employee = require_employee(emp_code)
    record = logs.find_one({"emp_code": emp_code, "date": day})
    if record is None:
        raise not_found("no record for this employee and date")
    current = current_state(record)
    corrected = corrected_state(current, body, employee, day)
    if all(current[field] == corrected[field] for field in ("status", "punch_in", "punch_out")):
        raise unprocessable("the request changes nothing")
    changes = {
        field: {"from": current[field], "to": corrected[field]}
        for field in current
        if current[field] != corrected[field]
    }
    entry = {"at": datetime.now(UTC), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    updated = logs.find_one_and_update(
        concurrency_guard(record),
        {"$set": corrected, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise conflict("the record was modified concurrently, retry")
    return attendance_out(updated)


IS_PRESENT = {"$in": ["$status", PRESENCE_STATUSES]}
IS_WEEKDAY = {
    "$in": [
        {"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d", "onError": None}}},
        [1, 2, 3, 4, 5],
    ]
}
LATE_MINUTES = {"$ifNull": ["$late_minutes", 0]}
IS_LATE = {"$gt": [LATE_MINUTES, 0]}
DAY_WEIGHT = {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}
PRESENT_WEIGHT = {"$cond": [IS_PRESENT, DAY_WEIGHT, 0]}
WEEKDAY_PRESENT_WEIGHT = {"$cond": [{"$and": [IS_PRESENT, IS_WEEKDAY]}, DAY_WEIGHT, 0]}
HAS_HOURS = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}


def count_if(condition: dict) -> dict:
    return {"$sum": {"$cond": [condition, 1, 0]}}


MONTH_STATS = {
    "present_days": {"$sum": WEEKDAY_PRESENT_WEIGHT},
    "leave_count": count_if({"$eq": ["$status", "LEAVE"]}),
    "on_duty_count": count_if({"$eq": ["$status", "ON_DUTY"]}),
    "late_count": count_if(IS_LATE),
    "late_minutes": {"$sum": {"$cond": [IS_LATE, LATE_MINUTES, 0]}},
    "overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
    "wh_cents": {"$sum": {"$cond": [HAS_HOURS, {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}, 0]}},
    "wh_count": count_if(HAS_HOURS),
}
ZERO_STATS = {key: 0 for key in MONTH_STATS}


def date_range(first: Date, last: Date) -> dict:
    return {"$gte": first.isoformat(), "$lte": last.isoformat()}


def monthly_pipeline(emp_code: str, first: Date, last: Date) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": date_range(first, last)}},
        {"$group": {"_id": None, **MONTH_STATS}},
    ]


def summary_pipeline(first: Date, last: Date, department: Optional[str]) -> list:
    employee_filter: dict[str, Any] = {"joined_on": {"$lte": last.isoformat()}}
    if department:
        employee_filter["department"] = department
    return [
        {"$match": employee_filter},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": date_range(first, last)}},
                    {"$group": {"_id": None, **MONTH_STATS}},
                ],
                "as": "stats",
            }
        },
        {"$set": {"stats": {"$ifNull": [{"$first": "$stats"}, ZERO_STATS]}}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                **{key: {"$sum": f"$stats.{key}"} for key in ZERO_STATS},
            }
        },
        {"$sort": {"_id": 1}},
    ]


def leaderboard_pipeline(first: Date, last: Date, limit: int, department: Optional[str]) -> list:
    pipeline: list[dict] = [
        {"$match": {"date": date_range(first, last), "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return pipeline


def department_codes(department: str) -> list[str]:
    codes = employees.distinct("emp_code", {"department": department})
    if not codes:
        raise not_found("department not found")
    return codes


def trend_pipeline(department: str, codes: list[str], first: Date, last: Date) -> list:
    lower = datetime.combine(first, time.min, UTC)
    upper = datetime.combine(last + timedelta(days=1), time.min, UTC)
    rounded_rate_units = {
        "$floor": {"$add": [{"$divide": [{"$multiply": ["$present_count", 10000]}, "$headcount"]}, 0.5]}
    }
    return [
        {"$match": {"emp_code": {"$in": codes}, "date": date_range(first, last)}},
        {"$group": {"_id": "$date", "present_count": {"$sum": PRESENT_WEIGHT}, "late_count": count_if(IS_LATE)}},
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$limit": 1},
                    {"$replaceWith": {"_id": first.isoformat(), "present_count": 0, "late_count": 0}},
                ],
            }
        },
        {"$group": {"_id": "$_id", "present_count": {"$sum": "$present_count"}, "late_count": {"$sum": "$late_count"}}},
        {"$set": {"day": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [lower, upper]}}},
        {
            "$set": {
                "date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d"}},
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "is_working_day": {"$in": [{"$isoDayOfWeek": "$day"}, [1, 2, 3, 4, 5]]},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": "$date"},
                "pipeline": [
                    {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}},
                    {"$count": "n"},
                ],
                "as": "joined",
            }
        },
        {"$set": {"headcount": {"$ifNull": [{"$first": "$joined.n"}, 0]}}},
        {
            "$set": {
                "rate_units": {
                    "$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]}, rounded_rate_units, None]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"day": 1},
                "output": {"avg_units": {"$avg": "$rate_units", "window": {"documents": [-6, "current"]}}},
            }
        },
        {"$sort": {"day": 1}},
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "rate_units": 1,
                "avg_units": 1,
            }
        },
    ]


def units_to_rate(units: Optional[float]) -> Optional[float]:
    if units is None:
        return None
    return float(Decimal(Decimal(units).quantize(Decimal(1), rounding=ROUND_HALF_UP)).scaleb(-4))


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    first, last = month_bounds(month)
    employee = require_employee(emp_code)
    stats = next(iter(logs.aggregate(monthly_pipeline(emp_code, first, last))), ZERO_STATS)
    working_days = working_days_between(max(first, Date.fromisoformat(employee["joined_on"])), last)
    present = Decimal(repr(float(stats["present_days"])))
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": half_up(float(stats["present_days"])),
        "leave_days": stats["leave_count"],
        "late_count": stats["late_count"],
        "total_late_minutes": stats["late_minutes"],
        "total_overtime_minutes": stats["overtime_minutes"],
        "attendance_pct": half_up(present * 100 / working_days) if working_days else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    first, last = month_bounds(month)
    items = []
    for row in employees.aggregate(summary_pipeline(first, last, department)):
        average = (
            half_up(Decimal(int(row["wh_cents"])) / Decimal(row["wh_count"]) / 100) if row["wh_count"] else None
        )
        items.append(
            {
                "department": row["_id"],
                "headcount": row["headcount"],
                "present_days": half_up(float(row["present_days"])),
                "avg_work_hours": average,
                "late_count": row["late_count"],
                "total_late_minutes": row["late_minutes"],
                "leave_count": row["leave_count"],
                "on_duty_count": row["on_duty_count"],
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: Annotated[int, Query(ge=1, le=50)] = 10, department: Optional[str] = None):
    first, last = month_bounds(month)
    return {"month": month, "items": list(logs.aggregate(leaderboard_pipeline(first, last, limit, department)))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: Annotated[str, Query(alias="from")], to: str):
    first, last = trend_window(from_, to)
    codes = department_codes(department)
    items = []
    for row in logs.aggregate(trend_pipeline(department, codes, first, last)):
        items.append(
            {
                "date": row["date"],
                "is_working_day": row["is_working_day"],
                "headcount": row["headcount"],
                "present_count": row["present_count"],
                "late_count": row["late_count"],
                "attendance_rate": units_to_rate(row["rate_units"]),
                "moving_avg_7d": units_to_rate(row["avg_units"]),
            }
        )
    return {"department": department, "items": items}


def run_explain(command: dict) -> dict:
    raw = db.command({"explain": command, "verbosity": "executionStats"})
    return json.loads(json_util.dumps(raw, json_options=json_util.RELAXED_JSON_OPTIONS))


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[str], Query(alias="from")] = None,
    to: Optional[str] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    if endpoint == "attendance_list":
        command = {
            "find": logs.name,
            "filter": attendance_query(emp_code, date_from, date_to, status),
            "sort": dict(ATTENDANCE_SORT),
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
        collection = logs.name
    elif endpoint == "department_trend":
        first, last = trend_window(require(from_, "from"), require(to, "to"))
        name = require(department, "department")
        command = {"aggregate": logs.name, "pipeline": trend_pipeline(name, department_codes(name), first, last), "cursor": {}}
        collection = logs.name
    else:
        first, last = month_bounds(require(month, "month"))
        if endpoint == "employee_monthly":
            pipeline, collection = monthly_pipeline(require(emp_code, "emp_code"), first, last), logs.name
        elif endpoint == "department_summary":
            pipeline, collection = summary_pipeline(first, last, department), employees.name
        else:
            pipeline, collection = leaderboard_pipeline(first, last, limit, department), logs.name
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    return {"endpoint": endpoint, "collection": collection, "explain": run_explain(command)}
