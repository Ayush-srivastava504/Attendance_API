# Architecture notes

Everything lives in `app/main.py`, laid out top to bottom so it reads like a short story: configuration and indexes, small time helpers, the business rules, serializers and request models, CRUD and attendance endpoints, the aggregation pipeline builders, the analytics endpoints, and finally explain.

## Time handling

* MongoDB stores UTC datetimes; the client is created with `tz_aware=True`, so nothing naive comes back. `aware()` still guards values from other sources.
* The API speaks epoch milliseconds. `ms_to_datetime` drops the sub-second part (R1) and `datetime_to_ms` converts back using integer arithmetic only, so no float ever touches an instant.
* Dates and shift times stay strings. `shift_window(day, start, end)` is the single place that turns them into IST datetimes, adding one day to the end of an overnight shift. `attendance_date` applies the R1 overnight exception.

## Rules in code

| Rule | Where | Detail |
|---|---|---|
| R2 late | `compute_late_minutes` | Compare whole seconds to 600; late means floor minutes since shift start. |
| R3 overtime | `compute_overtime_minutes` | Floor minutes after shift end; below 30 becomes 0. |
| R4 / R5 | `compute_work_hundredths` | `(2*seconds + 36) // 72` is exact half-up to hundredths; half day is `< 450`. |
| R8 | `half_up` | `Decimal` quantize with `ROUND_HALF_UP`. |

`derived_fields` is the only function that produces the four stored derived values, used by punch-in, punch-out and regularization.

## Concurrency

| Operation | Guard |
|---|---|
| Create employee, punch-in | Unique indexes; the loser gets `DuplicateKeyError` -> 409. |
| Punch-out | `find_one_and_update` filtered on `punch_out: null`; the loser matches nothing -> 409. |
| Regularize | `find_one_and_update` filtered on the punch times and the exact history length it read. A concurrent edit makes it match nothing -> 409, so no history entry is lost. |

## Analytics pipelines

Rounding is done after aggregation, in Python, on a handful of rows (Mongo's `$round` is half-even, the contract is half-up). Anything that could create a tie is kept exact: work hours are summed as integer hundredths, and trend rates as integer ten-thousandths.

* **Monthly**: `$match` on the unique `(emp_code, date)` index, one `$group`. Working days come from the calendar and `joined_on`.
* **Department summary**: starts from `employees` (`joined_on <= month end`), `$lookup`s the month's logs per person with a grouped sub-pipeline, then groups by department. Zero logs means zero stats and the person still counts.
* **Leaderboard**: group late minutes per employee, `$lookup` the employee, `$setWindowFields` with `$rank` for competition ranking, then `rank <= limit`.
* **Trend**: group the department's logs per day, add a seed row so an empty range still densifies, `$densify` one document per day, `$lookup` headcount per day from `employees`, then `$setWindowFields` with `$avg` over `documents: [-6, current]` for the moving average.

The `/admin/explain` endpoint calls the same builder functions as the real endpoints, so the plan it shows is the plan that runs.

## Extras beyond the contract

`GET /employees/{emp_code}`, `GET /attendance/{emp_code}/{date}`, an `X-Process-Time-Ms` header on every response, and a 503 handler for any MongoDB driver error. None of them change the specified behaviour.
