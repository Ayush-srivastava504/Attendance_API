# DECISIONS.md

1. **Indexes.** `employees`: unique `emp_code`; `(department, emp_code)` for the sorted list; `(department, joined_on)` and `(joined_on)` for headcount. `attendance_logs`: unique `(emp_code, date)` (one record per day, monthly stats, PATCH lookup); `(date desc, emp_code)` for the default list; `(status, date desc, emp_code)` for status filters; `(emp_code, punch_in desc)` to find the open record at punch-out. Rejected: a covering `(date, late_minutes, emp_code)` index for the leaderboard, since it costs writes for an already fast query.

2. **Punch-in race.** Both requests compute the same date and call `insert_one`. The unique `(emp_code, date)` index lets one through; the other hits `DuplicateKeyError` and gets 409. No read happens first, so nothing can slip in between.

3. **Ties.** I rank by total late minutes with `$rank`, then keep `rank <= limit`. Everyone tied at the cutoff is returned, so the list can exceed `limit`.

4. **Headcount.** The pipeline starts from `employees`, filtered by `joined_on`, then looks up each person's logs. No logs gives zero stats, but the person still counts once.

5. **100x data.** A daily per-department rollup updated on each write, so trend and summary stop scanning logs.
