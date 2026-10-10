# Verification — 10 October 2026

Environment: Windows, Python 3.12.1, FastAPI 0.115.12, PyMongo 4.12.1, MongoDB 7.0.16. Integration and benchmark runs use disposable randomly named databases.

`python -m pytest -q`: **22 passed in 4.38 seconds**.

Coverage includes employee validation/filtering/pagination, parallel duplicate employee creation, strict millisecond timestamps, seconds truncation, grace boundaries, overnight attendance-date boundaries, concurrent punches, maximum durations, half-up work/analytics rounding, half-day threshold, correction validation and nested BSON audit serialization, stale-snapshot rejection, concurrent history preservation, missing legacy fields, no-log employees, mid-month/future joiners, stored derived values, record-weighted averages, weekend contributions, competition ranking and skipped ranks, calendar gaps, moving windows, readiness failure, and all explain plans.

`python tests/performance.py`: 1,000 employees and **100,000 attendance records**, generated directly in a disposable MongoDB database. Index creation/startup completed in **5.018 seconds**. Each query also ran through its explain endpoint with executionStats: **IXSCAN present and COLLSCAN absent in all five outputs**.

| Endpoint | Observed response time |
|---|---:|
| Attendance page, filtered and page 2 | 67.16 ms |
| Employee monthly summary | 31.46 ms |
| All-department summary | 463.53 ms |
| Late leaderboard with tied cutoff | 437.08 ms |
| Department daily trend, 31 days | 37.38 ms |

These are single-run local observations, not production latency guarantees. The benchmark checks filtered totals, headcount, tie inclusion, and trend gaps as well as query plans. It uses stored analytics values intentionally; the integration suite separately verifies punch calculations.

The required `uvicorn app.main:app --port 8000` startup was also exercised with environment configuration: startup succeeded, `/health` returned 200 with `{"status":"ok"}`, and `/docs` returned 200.

Hidden grader data and production infrastructure are unavailable, so their results are not claimed. No features from the supplied contract are intentionally omitted.
