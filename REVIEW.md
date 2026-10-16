# REVIEW.md

Defects found in the starter `app/main.py`, in the order they appear. Every one is fixed in the new file.

| # | Where | What is wrong | How you'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `MongoClient(...)` | No `tz_aware`, so every datetime read back is naive. No server timeout. | Comparing a stored punch with an aware value raises `TypeError`; a down database hangs for 30 s. | `tz_aware=True, tzinfo=UTC`, `serverSelectionTimeoutMS=5000`. |
| 2 | `/health` | Always returns ok, never touches MongoDB. | Stop MongoDB: health still says 200. | Pings MongoDB; any driver error becomes 503. |
| 3 | `compute_late_minutes` | Builds shift start on the punch's own (UTC) date and clock, not IST. Floors to minutes before the `> 10` test, so 09:40:01 gives 0 instead of 10. No overnight handling. Seconds not truncated. | Punch-in at 10:15 IST (04:45 UTC) is compared with 09:30 UTC and never counts as late. 09:40:01 gives 0. | Shift window built in IST from the attendance date; compares whole seconds against 600 first, then floors. |
| 4 | `compute_work_hours` | Float `round()` (banker's/binary) instead of half-up. | 18 s of work should be 0.01 h. | Integer maths: `(2*seconds + 36) // 72` hundredths. |
| 5 | `compute_overtime` | Missing the 30-minute minimum. Uses the date string only, never the overnight next-day end. | Punch-out 10 min after shift end returned 10. An overnight shift reports about 1480 minutes. | Shift end taken from `shift_window` (adds a day for overnight); under 30 becomes 0. |
| 6 | `create_employee` | Check-then-insert race with no unique index. `created_at` is naive local time and returned as a `datetime`, not epoch ms. No validation of code format, shifts (`start == end`), email or `joined_on`. | Two parallel creates both get 201. `shift_start == shift_end`. | Unique index plus `DuplicateKeyError` -> 409. UTC `created_at`, ms in the response. Pydantic patterns and a model validator. |
| 7 | `list_employees` | `skip = page * page_size` skips the first page. `total` counts the whole collection, ignoring `department`. No sort. No bounds on `page` / `page_size`. | `page=1` returns the second page; total is wrong with a filter. | `(page-1)*page_size`, filtered `count_documents`, sort by `emp_code`, `Query(ge=1, le=100)`. |
| 8 | `punch_in` | Unknown employee: `emp[...]` on `None` gives a 500. `punched_at` of 0 is treated as "not sent". Local server timezone for date and time. Status is free text (`ABSENT` accepted). Seconds not truncated. Overnight date ignored. | Punch-in for `EMP9999`. `"status": "ABSENT"`. A server not on IST. | 404, strict epoch-ms validation (422), IST date via R1, status limited to the three presence values, truncation to seconds. |
| 9 | `punch_in` | Check-then-insert race. | Eight parallel identical requests give several 201s. | Unique `(emp_code, date)` index; `insert_one` and catch `DuplicateKeyError` -> 409. |
| 10 | `punch_in` response | Leaks the ObjectId as `id`, returns datetimes instead of epoch ms. | Look at the response body. | Single `attendance_out` serializer. |
| 11 | `list_attendance` | Loads the whole collection, sorts in Python, slices. No tie-break on `emp_code`. Leaks `_id`. No date validation (`date_from > date_to` accepted). No bounds on paging. No indexes. | 100k records: slow and memory heavy. | Server-side sort/skip/limit, supporting indexes, validated filters, `total` from the same query. |
| 12 | Models | `PunchInIn.status` is a bare `str`; `punched_at` accepts anything that parses as an int, including seconds. | `punched_at=1783312500` (seconds). | `Literal` status and a strict int range type for epoch ms. |
| 13 | Whole file | No indexes created at startup. | `explain` shows COLLSCAN. | `ensure_indexes()` at startup, idempotent, retried from `/health` if MongoDB was late. |

**Looked at and left alone:** `doc.pop("_id")` after `insert_one` is correct (the driver adds `_id` to the dict). `load_dotenv()` does not override real environment variables by default, which is what the contract asks for. Using sync endpoints is fine: FastAPI runs them in a threadpool and PyMongo is thread-safe.
