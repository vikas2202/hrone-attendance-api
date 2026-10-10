# Employee Attendance & Analytics API

FastAPI + MongoDB implementation of the supplied HROne v2 contract. All application code is in `app/main.py`. Includes audited corrections, race-safe punches, aggregation analytics, and execution-statistics explain endpoints.

Requires Python 3.11+ and MongoDB 6.0+ (tested with MongoDB 7.0.16).

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
# Set MONGO_URI and MONGO_DB in your environment, or copy .env.example to .env.
uvicorn app.main:app --port 8000
```

Open `/docs` for interactive API documentation. `/health` pings MongoDB. Startup creates indexes; the configured account needs index-creation permissions. Existing duplicate employee or attendance keys must be resolved before startup can create unique indexes. Environment variables override `.env`; no authentication is required by this assignment.

Run integration tests against a disposable local MongoDB instance:

```bash
pip install -r requirements-dev.txt
# PowerShell: $env:TEST_MONGO_URI = 'mongodb://localhost:27018'
# Bash: export TEST_MONGO_URI=mongodb://localhost:27018
pytest -q
```

Tests create and remove a randomly named test database only. They never clear your configured application database. `ANALYSIS.md`, `REVIEW.md`, and `DECISIONS.md` explain scope, starter defects, and implementation choices. `VALIDATION.md` records verification results. No frontend is required by the assignment, and the runtime makes no external calls other than MongoDB. No secrets, data dumps, Dockerfile, or virtual environment are included.
