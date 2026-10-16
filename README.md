# Employee Attendance & Analytics API

A FastAPI service over MongoDB for an HR attendance system: employees punch in and out, HR corrects records with a full audit trail, and managers query monthly summaries, a late-comers leaderboard and a daily department trend. All application code is in `app/main.py`.

## Quick start

Requirements: Python 3.11+, MongoDB 6.0+ (the aggregation uses `$densify` and `$setWindowFields`).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                 # MONGO_URI and MONGO_DB; real environment variables win
docker run -d -p 27017:27017 mongo:7 # skip if you already have MongoDB
python sample_seed.py                # optional: load the nine sample documents
uvicorn app.main:app --port 8000
```

Open `http://localhost:8000/docs` for the interactive API. Indexes are created at startup and the call is idempotent, so there are no manual steps.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `MONGO_URI` | `mongodb://localhost:27017` | Connection string (Atlas `mongodb+srv://` works) |
| `MONGO_DB` | `attendance_db` | Database holding `employees` and `attendance_logs` |

## What is implemented

Every endpoint in `openapi.yaml`: health, employees (create, list), punch-in, punch-out, attendance list, regularization, the four analytics endpoints and `/admin/explain/{endpoint}`. Business rules R1-R10 are implemented as written; the code for each rule is mapped in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

Extras that do not change the contract: `GET /employees/{emp_code}`, `GET /attendance/{emp_code}/{date}`, an `X-Process-Time-Ms` response header, and a 503 response for any MongoDB driver failure.

## Project layout

```
app/main.py            the whole service
tests/                 rule unit tests, HTTP tests, live analytics tests
docs/API.md            endpoint reference and examples
docs/ARCHITECTURE.md   how time, rules, concurrency and pipelines work
REVIEW.md              defects found in the starter
DECISIONS.md           design answers
openapi.yaml           the contract
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest                                            # rules + HTTP flows on an in-memory MongoDB double
TEST_MONGO_URI=mongodb://localhost:27017 pytest   # also runs the analytics and explain tests
```

The analytics tests insert a small hand-checked dataset (a tie at the leaderboard cutoff, an employee with no logs, a mid-month joiner, a weekend record, a half day, a quiet department) into a separate database called `attendance_analytics_test` and drop it first.


