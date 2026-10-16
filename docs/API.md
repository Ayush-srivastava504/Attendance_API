# API reference

The machine-readable contract is `openapi.yaml`; FastAPI also serves live docs at `/docs`. This page is the short human version, with the behaviour that is easy to get wrong.

Conventions: instants are **epoch milliseconds** (integers between 1e11 and 4.1e12; seconds, floats and strings give 422). Dates are `YYYY-MM-DD` (IST), months `YYYY-MM`. Employees are addressed by `emp_code`, never by `_id`. Errors are `{"detail": ...}`. Every response carries an `X-Process-Time-Ms` header.

| Method and path | Purpose | Notable behaviour |
|---|---|---|
| `GET /health` | Readiness | 200 only if MongoDB answers a ping, otherwise 503. |
| `POST /employees` | Create | Unique `emp_code` (`EMP` + 4-6 digits) -> 409 on duplicate; `shift_start != shift_end`. |
| `GET /employees` | List | Sorted by `emp_code`; `department` filter; `total` follows the filter. |
| `GET /employees/{emp_code}` | Fetch one *(extra)* | 404 if unknown. |
| `POST /attendance/punch-in` | Start the day | Date by R1, lateness by R2. One 201 per (employee, date), the rest 409. |
| `POST /attendance/punch-out` | Close the latest open record | Works across midnight. 404 none found, 409 already closed, 422 not after punch-in or over 24 h. |
| `GET /attendance` | List | Sorted by `date` desc then `emp_code`; filters `emp_code`, `date_from`, `date_to`, `status`. |
| `GET /attendance/{emp_code}/{date}` | Fetch one *(extra)* | 404 if no record. |
| `PATCH /attendance/{emp_code}/{date}` | Regularize | Recomputes derived fields, appends one history entry, 409 if someone else edited first. |
| `GET /analytics/employees/{emp_code}/monthly` | Employee month | `working_days` from `joined_on`; half day = 0.5; Mon-Fri only for `present_days`. |
| `GET /analytics/departments/summary` | Department month | Headcount includes people with no logs. |
| `GET /analytics/leaderboard/late` | Late ranking | Competition ranking (1, 2, 2, 4); `limit` applies to rank. |
| `GET /analytics/departments/{department}/trend` | Daily series | One row per calendar day, 7-day moving average inside the range, max 92 days. |
| `GET /admin/explain/{endpoint}` | Query plan | Raw `executionStats` for the exact query the endpoint runs. |

## Examples

```bash
curl -X POST localhost:8000/employees -H 'content-type: application/json' \
  -d '{"emp_code":"EMP0010","name":"Isha Verma","email":"isha@example.com","department":"Sales","joined_on":"2026-07-01"}'

curl -X POST localhost:8000/attendance/punch-in -H 'content-type: application/json' \
  -d '{"emp_code":"EMP0010","punched_at":1783312500000,"status":"WFH"}'

curl -X PATCH localhost:8000/attendance/EMP0010/2026-07-06 -H 'content-type: application/json' \
  -d '{"punch_in":1783310280000,"reason":"biometric glitch","regularized_by":"hr.admin"}'

curl 'localhost:8000/analytics/leaderboard/late?month=2026-07&limit=3'
curl 'localhost:8000/analytics/departments/Sales/trend?from=2026-07-01&to=2026-07-31'
curl 'localhost:8000/admin/explain/department_summary?month=2026-07'
```

## Status codes at a glance

| Code | When |
|---|---|
| 201 | Employee or punch-in created |
| 404 | Unknown employee, no record for that date, no punch-in to close, unknown department |
| 409 | Duplicate `emp_code`, duplicate punch-in, already punched out, concurrent regularization |
| 422 | Any validation failure, including bad timestamps, bad dates, empty corrections |
| 503 | MongoDB unreachable |
