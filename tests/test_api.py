"""Integration tests use a disposable, randomly named MongoDB database."""
import importlib
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))


def ms(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=IST).timestamp() * 1000)


@pytest.fixture(scope="session")
def service():
    uri = os.getenv("TEST_MONGO_URI", "mongodb://localhost:27018")
    name = "attendance_test_" + uuid4().hex
    connection = MongoClient(uri, tz_aware=True, serverSelectionTimeoutMS=3000)
    connection.admin.command("ping")
    os.environ.update(MONGO_URI=uri, MONGO_DB=name)
    main = importlib.import_module("app.main")
    with TestClient(main.app) as api:
        yield api, connection[name], main
    connection.drop_database(name)
    connection.close()


@pytest.fixture(autouse=True)
def clean(service):
    _, database, _ = service
    database.employees.delete_many({})
    database.attendance_logs.delete_many({})


def emp(code="EMP0001", department="Engineering", **kw):
    return dict(emp_code=code, name=code, email=code + "@example.com", department=department,
                joined_on="2026-01-01", **kw)


def create(api, **kw):
    body = emp(**kw)
    result = api.post("/employees", json=body)
    assert result.status_code == 201, result.text
    return body


def log(database, code, day, **kw):
    record = dict(emp_code=code, date=day, status="PRESENT", punch_in=None, punch_out=None,
                  work_hours=None, late_minutes=0, overtime_minutes=0)
    record.update(kw)
    database.attendance_logs.insert_one(record)


def test_employee_validation_pagination_and_races(service):
    api, _, _ = service
    for field, value in [("emp_code", "EMP1"), ("email", "invalid"), ("joined_on", "2026-02-30"),
                         ("shift_start", "25:00"), ("shift_end", "09:30"), ("name", "")]:
        assert api.post("/employees", json={**emp(), field: value}).status_code == 422
    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(lambda _: api.post("/employees", json=emp()).status_code, range(8)))
    assert sorted(codes) == [201] + [409] * 7
    create(api, code="EMP0002", department="Other")
    create(api, code="EMP0003")
    response = api.get("/employees", params={"department": "Engineering", "page_size": 1}).json()
    assert response["total"] == 2 and response["items"][0]["emp_code"] == "EMP0001"
    assert isinstance(response["items"][0]["created_at"], int)
    assert "_id" not in response["items"][0]
    assert api.get("/employees?page=0").status_code == 422
    assert api.get("/employees?page_size=101").status_code == 422


@pytest.mark.parametrize("value", [1783312500, 1783312500000.0, "1783312500000", True, None, 4102444800001])
def test_strict_timestamps(service, value):
    api, _, _ = service
    create(api)
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": value}).status_code == 422


@pytest.mark.parametrize("clock,late", [("09:40:00", 0), ("09:40:01", 10), ("10:15:59", 45)])
def test_late_boundary_truncation(service, clock, late):
    api, _, _ = service
    create(api)
    ts = ms("2026-07-06T" + clock) + 999
    r = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ts}).json()
    assert r["late_minutes"] == late and r["punch_in"] == ts - 999
    assert r["history"] == [] and "id" not in r and "_id" not in r


def test_overnight_and_punch_races(service):
    api, _, _ = service
    create(api, shift_start="22:00", shift_end="06:00")
    body = dict(emp_code="EMP0001", punched_at=ms("2026-07-07T00:10:00"))
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: api.post("/attendance/punch-in", json=body), range(8)))
    assert sorted(r.status_code for r in results) == [201] + [409] * 7
    doc = next(r.json() for r in results if r.status_code == 201)
    assert doc["date"] == "2026-07-06" and doc["late_minutes"] == 130
    body["punched_at"] = ms("2026-07-07T06:40:00")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: api.post("/attendance/punch-out", json=body), range(8)))
    assert sorted(r.status_code for r in results) == [200] + [409] * 7
    doc = next(r.json() for r in results if r.status_code == 200)
    assert doc["overtime_minutes"] == 40 and doc["work_hours"] == 6.5 and not doc["half_day"]
    assert doc["history"] == []


def test_half_up_and_overtime(service):
    api, _, main = service
    assert main.compute_work_hours(datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, 0, 18, 54, tzinfo=UTC)) == 0.32
    create(api)
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06T09:30:00")})
    for ts in ["2026-07-06T09:30:00", "2026-07-07T09:30:01"]:
        assert api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms(ts)}).status_code == 422
    result = api.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06T13:59:42")}).json()
    assert result["work_hours"] == 4.5 and result["half_day"] is False
    assert result["overtime_minutes"] == 0


def test_corrections_history_and_concurrency(service):
    api, database, main = service
    create(api)
    api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06T10:05:00")})
    path = "/attendance/EMP0001/2026-07-06"
    audit = dict(reason="biometric glitch", regularized_by="hr.admin")
    assert api.patch(path, json=audit).status_code == 422
    assert api.patch(path, json={**audit, "punch_in": None}).status_code == 422
    assert api.patch(path, json={**audit, "punch_in": ms("2026-07-07T09:30:00")}).status_code == 422
    result = api.patch(path, json={**audit, "punch_in": ms("2026-07-06T09:28:00"), "late_minutes": 999}).json()
    assert result["late_minutes"] == 0
    assert set(result["history"][0]["changes"]) == {"punch_in", "late_minutes"}
    assert isinstance(result["history"][0]["changes"]["punch_in"]["from"], int)
    stored = database.attendance_logs.find_one({"emp_code": "EMP0001"})
    assert isinstance(stored["history"][0]["changes"]["punch_in"]["to"], datetime)
    # Check the compare-and-swap rejects a stale snapshot after an audit append.
    result = api.patch(path, json={**audit, "status": "WFH"})
    assert result.status_code == 200
    assert database.attendance_logs.update_one(main.snapshot_filter(stored), {"$set": {"status": "ON_DUTY"}}).matched_count == 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        edits = list(pool.map(lambda n: api.patch(path, json={**audit, "punch_out": ms("2026-07-06T18:30:00") + n * 60000}), range(8)))
    successes = sum(r.status_code == 200 for r in edits)
    assert all(r.status_code in [200, 409] for r in edits)
    assert len(database.attendance_logs.find_one({"emp_code": "EMP0001"})["history"]) == 2 + successes
    assert api.patch(path, json={**audit, "status": "LEAVE", "punch_out": ms("2026-07-06T18:30:00")}).status_code == 422
    result = api.patch(path, json={**audit, "status": "LEAVE"}).json()
    assert result["punch_in"] is None and result["punch_out"] is None and result["late_minutes"] == 0


def seed_analytics(api, database):
    for code, kw in [("EMP0001", {}), ("EMP0002", {}), ("EMP0003", {}),
                     ("EMP0004", {"joined_on": "2026-07-15"}), ("EMP0005", {"joined_on": "2026-08-01"})]:
        body = emp(code)
        body.update(kw)
        assert api.post("/employees", json=body).status_code == 201
    create(api, code="EMP0006", department="Empty")
    log(database, "EMP0001", "2026-07-06", work_hours=8.0, late_minutes=30)
    log(database, "EMP0001", "2026-07-07", work_hours=6.0)
    log(database, "EMP0001", "2026-07-11", work_hours=3.0, late_minutes=10, half_day=True, overtime_minutes=45)
    log(database, "EMP0002", "2026-07-06", status="WFH", work_hours=3.5, half_day=True, late_minutes=20)
    log(database, "EMP0003", "2026-07-06", status="ON_DUTY", work_hours=9.0, late_minutes=20)
    log(database, "EMP0002", "2026-07-08", status="LEAVE")
    log(database, "EMP9999", "2026-07-06", late_minutes=1000)


def test_analytics_authoritative_values_and_ties(service):
    api, database, _ = service
    seed_analytics(api, database)
    result = api.get("/analytics/employees/EMP0001/monthly?month=2026-07").json()
    assert result == dict(emp_code="EMP0001", month="2026-07", working_days=23, present_days=2,
                         leave_days=0, late_count=2, total_late_minutes=40, total_overtime_minutes=45, attendance_pct=8.7)
    joiner = api.get("/analytics/employees/EMP0004/monthly?month=2026-07").json()
    assert joiner["working_days"] == 13 and joiner["present_days"] == 0
    future = api.get("/analytics/employees/EMP0005/monthly?month=2026-07").json()
    assert future["working_days"] == 0 and future["attendance_pct"] is None
    items = api.get("/analytics/departments/summary?month=2026-07").json()["items"]
    assert items[0]["department"] == "Empty" and items[0]["headcount"] == 1 and items[0]["avg_work_hours"] is None
    eng = items[1]
    assert eng["headcount"] == 4 and eng["present_days"] == 3.5 and eng["avg_work_hours"] == 5.9
    assert eng["total_late_minutes"] == 80 and eng["leave_count"] == 1 and eng["on_duty_count"] == 1
    rows = api.get("/analytics/leaderboard/late?month=2026-07&limit=2").json()["items"]
    assert [(r["emp_code"], r["rank"]) for r in rows] == [("EMP0001", 1), ("EMP0002", 2), ("EMP0003", 2)]
    assert api.get("/analytics/leaderboard/late?month=2026-07&department=Empty").json()["items"] == []


def test_trend_gaps_weekends_joiners_and_window(service):
    api, database, _ = service
    seed_analytics(api, database)
    rows = api.get("/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-15").json()["items"]
    assert len(rows) == 10
    assert rows[0]["headcount"] == 3 and rows[0]["present_count"] == 2.5
    assert rows[0]["attendance_rate"] == 0.8333 and rows[0]["moving_avg_7d"] == 0.8333
    assert rows[1]["moving_avg_7d"] == 0.5833
    assert rows[5]["present_count"] == 0.5 and rows[5]["attendance_rate"] is None
    assert rows[-1]["headcount"] == 4
    assert rows[-1]["moving_avg_7d"] == 0.0
    empty = api.get("/analytics/departments/Empty/trend?from=2026-07-11&to=2026-07-13").json()["items"]
    assert [r["attendance_rate"] for r in empty] == [None, None, 0.0]
    assert [r["moving_avg_7d"] for r in empty] == [None, None, 0.0]
    assert api.get("/analytics/departments/Unknown/trend?from=2026-07-06&to=2026-07-07").status_code == 404


def test_list_filters_validation_legacy_and_errors(service):
    api, database, _ = service
    seed_analytics(api, database)
    result = api.get("/attendance?date_from=2026-07-06&date_to=2026-07-06&page_size=2&page=2").json()
    assert result["total"] == 4
    assert [r["emp_code"] for r in result["items"]] == ["EMP0003", "EMP9999"]
    assert result["items"][0]["half_day"] is False and result["items"][0]["history"] == []
    for url in ["/attendance?date_from=2026-02-30", "/attendance?status=INVALID", "/attendance?date_from=2026-07-10&date_to=2026-07-01",
                "/analytics/departments/summary?month=2026-13", "/analytics/departments/summary?month=0000-01",
                "/analytics/departments/Empty/trend?from=2026-01-01&to=2026-04-03", "/admin/explain/employee_monthly?month=2026-07",
                "/admin/explain/department_trend?department=Empty", "/admin/explain/invalid"]:
        assert api.get(url).status_code == 422, url
    assert api.post("/attendance/punch-in", json={"emp_code": "EMP9999"}).status_code == 404
    assert api.post("/attendance/punch-out", json={"emp_code": "EMP0006"}).status_code == 404
    assert api.get("/analytics/employees/EMP9999/monthly?month=2026-07").status_code == 404


def test_explain_indexes(service):
    api, database, _ = service
    seed_analytics(api, database)
    for endpoint, params in [("attendance_list", {"status": "WFH", "page": 2}),
                             ("employee_monthly", {"emp_code": "EMP0001", "month": "2026-07"}),
                             ("department_summary", {"month": "2026-07"}),
                             ("late_leaderboard", {"month": "2026-07", "limit": 2}),
                             ("department_trend", {"department": "Engineering", "from": "2026-07-06", "to": "2026-07-15"})]:
        response = api.get("/admin/explain/" + endpoint, params=params)
        assert response.status_code == 200, response.text
        text = response.text
        assert 'IXSCAN' in text and 'COLLSCAN' not in text, endpoint
        assert 'executionStats' in text


def test_readiness_failure(service, monkeypatch):
    api, _, main = service
    from pymongo.errors import ServerSelectionTimeoutError
    def fail(*args, **kwargs):
        raise ServerSelectionTimeoutError("test outage")
    with monkeypatch.context() as patch:
        patch.setattr(main.db, "command", fail)
        assert api.get("/health").status_code == 503
    assert api.get("/health").status_code == 200


def test_report_rounding_and_skipped_ranks(service):
    api, database, _ = service
    for i in range(1, 33):
        create(api, code=f"EMP{i:04d}")
    log(database, "EMP0001", "2026-07-06", work_hours=0.30, late_minutes=30)
    log(database, "EMP0001", "2026-07-11", work_hours=0.31)
    log(database, "EMP0002", "2026-07-11", status="LEAVE", late_minutes=20)
    log(database, "EMP0003", "2026-07-11", status="LEAVE", late_minutes=20)
    log(database, "EMP0004", "2026-07-11", status="LEAVE", late_minutes=10)
    summary = api.get("/analytics/departments/summary?month=2026-07").json()["items"][0]
    assert summary["avg_work_hours"] == 0.31
    rows = api.get("/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-07").json()["items"]
    assert rows[0]["attendance_rate"] == 0.0313 and rows[1]["moving_avg_7d"] == 0.0157
    ranks = api.get("/analytics/leaderboard/late?month=2026-07&limit=4").json()["items"]
    assert [r["rank"] for r in ranks] == [1, 2, 2, 4]
    assert len(api.get("/analytics/leaderboard/late?month=2026-07&limit=3").json()["items"]) == 3


@pytest.mark.parametrize("clock,expected", [("05:59:59", "2026-07-06"), ("06:00:00", "2026-07-07"),
                                           ("21:55:00", "2026-07-07")])
def test_overnight_date_end_boundary(service, clock, expected):
    api, _, _ = service
    create(api, shift_start="22:00", shift_end="06:00")
    response = api.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-07T" + clock)})
    assert response.status_code == 201 and response.json()["date"] == expected
