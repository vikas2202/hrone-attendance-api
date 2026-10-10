"""Optional 100k-record benchmark; only touches a disposable test database.

Run: python tests/performance.py
Set TEST_MONGO_URI if MongoDB is not listening on localhost:27018.
"""
import importlib
import json
import os
import pathlib
import sys
from datetime import date, datetime, timedelta, timezone
from time import perf_counter
from uuid import uuid4

from fastapi.testclient import TestClient
from pymongo import MongoClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
uri = os.getenv("TEST_MONGO_URI", "mongodb://localhost:27018")
name = "attendance_benchmark_" + uuid4().hex
connection = MongoClient(uri, tz_aware=True, serverSelectionTimeoutMS=4000)
database = connection[name]
os.environ.update(MONGO_URI=uri, MONGO_DB=name)
now = datetime.now(timezone.utc).replace(microsecond=0)
try:
    employees = [{"emp_code": f"EMP{i:04d}", "name": f"Employee {i}", "email": f"employee{i}@example.com",
                  "department": f"Department{i % 10}", "shift_start": "09:30", "shift_end": "18:30",
                  "joined_on": "2026-01-01", "created_at": now} for i in range(1000)]
    database.employees.insert_many(employees)
    first = date(2026, 4, 1)
    for offset in range(100):
        day = (first + timedelta(days=offset)).isoformat()
        logs = [{"emp_code": emp["emp_code"], "date": day, "status": "PRESENT", "punch_in": now,
                 "punch_out": now, "work_hours": 8.0, "late_minutes": (i % 5) * 10,
                 "overtime_minutes": 0, "half_day": False, "history": []} for i, emp in enumerate(employees)]
        database.attendance_logs.insert_many(logs)
    assert database.attendance_logs.count_documents({}) == 100000
    main = importlib.import_module("app.main")
    results = []
    started = perf_counter()
    with TestClient(main.app) as api:
        startup_seconds = round(perf_counter() - started, 3)
        queries = [
            ("attendance_list", "/attendance", {"date_from": "2026-07-01", "date_to": "2026-07-09", "page": 2}),
            ("employee_monthly", "/analytics/employees/EMP0001/monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
            ("department_summary", "/analytics/departments/summary", {"month": "2026-07"}),
            ("late_leaderboard", "/analytics/leaderboard/late", {"month": "2026-07", "limit": 10}),
            ("department_trend", "/analytics/departments/Department1/trend", {"department": "Department1", "from": "2026-07-01", "to": "2026-07-31"}),
        ]
        for endpoint, path, params in queries:
            started = perf_counter()
            response = api.get(path, params=params)
            elapsed = round((perf_counter() - started) * 1000, 2)
            assert response.status_code == 200, response.text
            explain = api.get("/admin/explain/" + endpoint, params=params)
            assert explain.status_code == 200, explain.text
            assert "IXSCAN" in explain.text and "COLLSCAN" not in explain.text
            body = response.json()
            if endpoint == "attendance_list":
                assert body["total"] == 9000 and len(body["items"]) == 20
            elif endpoint == "department_summary":
                assert len(body["items"]) == 10 and all(row["headcount"] == 100 for row in body["items"])
            elif endpoint == "late_leaderboard":
                assert len(body["items"]) == 200 and all(row["rank"] == 1 for row in body["items"])
            elif endpoint == "department_trend":
                assert len(body["items"]) == 31 and body["items"][-1]["attendance_rate"] == 0.0
            results.append({"endpoint": endpoint, "response_ms": elapsed, "IXSCAN": True, "COLLSCAN": False})
    print(json.dumps({"mongo_version": connection.server_info()["version"], "attendance_records": 100000,
                      "startup_seconds": startup_seconds, "results": results}, indent=2))
finally:
    connection.drop_database(name)
    connection.close()
