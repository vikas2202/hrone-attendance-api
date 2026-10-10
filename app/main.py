"""Contract v2 attendance API. All runtime application code lives in this file."""
import calendar
import os
import re
from contextlib import asynccontextmanager
from datetime import date as Date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal

from bson import Decimal128, ObjectId
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv(override=False)
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"),
                     tz_aware=True, serverSelectionTimeoutMS=4000, connectTimeoutMS=4000)
db = client[os.getenv("MONGO_DB", "attendance_db")]


def create_indexes():
    db.employees.create_index([("emp_code", 1)], unique=True, name="employee_code")
    db.employees.create_index([("department", 1), ("emp_code", 1)], name="employee_department")
    db.employees.create_index([("joined_on", 1), ("department", 1)], name="employee_joined")
    db.attendance_logs.create_index([("emp_code", 1), ("date", -1)], unique=True, name="attendance_key")
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="attendance_date")
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)], name="attendance_status")
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)], name="attendance_punch")


@asynccontextmanager
async def lifespan(app):
    # Fail startup rather than serve writes without their uniqueness guarantees.
    create_indexes()
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


def invalid(message):
    raise HTTPException(422, message)


def calendar_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("Expected YYYY-MM-DD")
    Date.fromisoformat(value)
    return value


def calendar_month(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", value):
        raise ValueError("Expected YYYY-MM")
    Date.fromisoformat(value + "-01")
    return value


Day = Annotated[str, BeforeValidator(calendar_date)]
Month = Annotated[str, BeforeValidator(calendar_month)]
Epoch = Annotated[int, Field(strict=True, ge=100000000000, le=4102444800000)]
Shift = Annotated[str, Field(strict=True, pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]
Limit = Annotated[int, Query(ge=1, le=50)]


class Input(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)


class EmployeeIn(Input):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: Shift = "09:30"
    shift_end: Shift = "18:30"
    joined_on: Day

    @model_validator(mode="after")
    def distinct_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchOutIn(Input):
    emp_code: str
    punched_at: Epoch = Field(default_factory=lambda: int(datetime.now(UTC).timestamp() * 1000))


class PunchInIn(PunchOutIn):
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class RegularizeIn(Input):
    # Default None means omitted, not an accepted explicit JSON null.
    status: Status = None
    punch_in: Epoch = None
    punch_out: Epoch = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


def instant(ms):
    return datetime.fromtimestamp(ms // 1000, UTC)


def aware(dt):
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def json_value(value):
    """Convert BSON at the boundary, including nested audit values and explain."""
    if isinstance(value, datetime):
        return int(aware(value).timestamp() * 1000)
    if isinstance(value, (Decimal128, Decimal)):
        return float(value.to_decimal() if isinstance(value, Decimal128) else value)
    if isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, dict):
        return {k: json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    return value


def employee_response(doc):
    return json_value({k: doc[k] for k in ("emp_code", "name", "email", "department",
                       "shift_start", "shift_end", "joined_on", "created_at")})


DEFAULTS = {"punch_in": None, "punch_out": None, "work_hours": None,
            "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []}


def attendance_response(doc):
    return json_value({k: doc.get(k, DEFAULTS.get(k)) for k in
                       ("emp_code", "date", "status", *DEFAULTS)})


def employee(code):
    result = db.employees.find_one({"emp_code": code})
    if result is None:
        raise HTTPException(404, "Unknown employee")
    return result


def attendance_day(ts, emp):
    local = aware(ts).astimezone(IST)
    day = local.date()
    if emp["shift_end"] <= emp["shift_start"] and local.time() < time.fromisoformat(emp["shift_end"]):
        day -= timedelta(days=1)
    return day.isoformat()


def shift_bounds(day, emp):
    start = datetime.combine(Date.fromisoformat(day), time.fromisoformat(emp["shift_start"]), IST)
    end = datetime.combine(Date.fromisoformat(day), time.fromisoformat(emp["shift_end"]), IST)
    if emp["shift_end"] <= emp["shift_start"]:
        end += timedelta(days=1)
    return start, end


def round_half_up(value, places=2):
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def compute_work_hours(punch_in, punch_out):
    seconds = Decimal(str((aware(punch_out) - aware(punch_in)).total_seconds()))
    return round_half_up(seconds / Decimal(3600))


def compute_late_minutes(punch_in, start):
    seconds = (aware(punch_in) - start).total_seconds()
    return int(seconds // 60) if seconds > 600 else 0


def compute_overtime(punch_out, end):
    minutes = int((aware(punch_out) - end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def derived(doc, emp):
    if doc["status"] not in PRESENCE:
        return {k: v for k, v in DEFAULTS.items() if k != "history"}
    pi, po = doc.get("punch_in"), doc.get("punch_out")
    if pi is None:
        invalid("Presence status requires punch_in")
    start, end = shift_bounds(doc["date"], emp)
    values = {"punch_in": pi, "punch_out": po, "work_hours": None,
              "late_minutes": compute_late_minutes(pi, start), "overtime_minutes": 0, "half_day": False}
    if po is not None:
        seconds = (aware(po) - aware(pi)).total_seconds()
        if seconds <= 0 or seconds > 86400:
            invalid("punch_out must be after punch_in and within 24 hours")
        values["work_hours"] = compute_work_hours(pi, po)
        values["overtime_minutes"] = compute_overtime(po, end)
        values["half_day"] = values["work_hours"] < 4.5
    return values


def snapshot_filter(doc):
    # Match exact stored state (including absent fields) without adding a schema version.
    # History equality prevents ABA corrections and makes append + update atomic.
    return {"_id": doc["_id"], **{k: doc[k] if k in doc else {"$exists": False}
            for k in ("status", *DEFAULTS)}}


@app.get("/health")
def health():
    try:
        db.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC).replace(microsecond=0)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_response(doc)


@app.get("/employees")
def list_employees(department: str | None = None, page: Page = 1, page_size: PageSize = 20):
    query = {} if department is None else {"department": department}
    items = db.employees.find(query).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_response(d) for d in items], "total": db.employees.count_documents(query),
            "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = employee(body.emp_code)
    ts = instant(body.punched_at)
    doc = {"emp_code": body.emp_code, "date": attendance_day(ts, emp), "status": body.status,
           "punch_in": ts, "punch_out": None, "history": []}
    doc.update(derived(doc, emp))
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Attendance date already exists")
    return attendance_response(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = employee(body.emp_code)
    ts = instant(body.punched_at)
    doc = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": ts, "$ne": None}},
                                       sort=[("punch_in", -1)])
    if doc is None:
        raise HTTPException(404, "No punch-in found")
    if doc.get("punch_out") is not None:
        raise HTTPException(409, "Already punched out")
    values = derived({**doc, "punch_out": ts}, emp)
    result = db.attendance_logs.find_one_and_update(snapshot_filter(doc), {"$set": values},
                                                    return_document=ReturnDocument.AFTER)
    if result is None:
        raise HTTPException(409, "Record changed concurrently")
    return attendance_response(result)


def attendance_query(emp_code=None, date_from=None, date_to=None, status=None):
    if date_from and date_to and date_from > date_to:
        invalid("date_from must not exceed date_to")
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if status is not None:
        query["status"] = status
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from
        if date_to is not None:
            query["date"]["$lte"] = date_to
    hint = "attendance_key" if emp_code is not None else "attendance_status" if status is not None else "attendance_date"
    return query, hint


@app.get("/attendance")
def list_attendance(emp_code: str | None = None, date_from: Day | None = None, date_to: Day | None = None,
                    status: Status | None = None, page: Page = 1, page_size: PageSize = 20):
    query, hint = attendance_query(emp_code, date_from, date_to, status)
    docs = db.attendance_logs.find(query).hint(hint).sort([("date", -1), ("emp_code", 1)]).skip(
        (page - 1) * page_size).limit(page_size)
    return {"items": [attendance_response(d) for d in docs],
            "total": db.attendance_logs.count_documents(query, hint=hint), "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: Day, body: RegularizeIn):
    emp = employee(emp_code)
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if old is None:
        raise HTTPException(404, "No record for attendance date")
    fields = body.model_fields_set
    new = dict(old)
    if "status" in fields:
        new["status"] = body.status
    if new["status"] not in PRESENCE:
        if fields & {"punch_in", "punch_out"}:
            invalid("ABSENT/LEAVE cannot include punch times")
        new.update(punch_in=None, punch_out=None)
    else:
        for key in ("punch_in", "punch_out"):
            if key in fields:
                new[key] = instant(getattr(body, key))
        if new.get("punch_in") is None:
            invalid("Presence status requires punch_in")
        if attendance_day(new["punch_in"], emp) != date:
            invalid("punch_in must stay on the attendance date")
    values = {"status": new["status"], **derived(new, emp)}
    changes = {k: {"from": old.get(k, DEFAULTS.get(k)), "to": v} for k, v in values.items()
               if old.get(k, DEFAULTS.get(k)) != v}
    if not changes:
        invalid("Correction changes nothing")
    entry = {"at": datetime.now(UTC).replace(microsecond=0), "by": body.regularized_by,
             "reason": body.reason, "changes": changes}
    result = db.attendance_logs.find_one_and_update(snapshot_filter(old),
             {"$set": values, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER)
    if result is None:
        raise HTTPException(409, "Record changed concurrently")
    return attendance_response(result)


def month_bounds(month):
    first = Date.fromisoformat(month + "-01")
    last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    return first.isoformat(), last.isoformat()


def mongo_day(value):
    return {"$dateFromString": {"dateString": value, "format": "%Y-%m-%d", "timezone": "+05:30"}}


def weekday(value):
    return {"$in": [{"$dayOfWeek": {"date": value, "timezone": "+05:30"}}, [2, 3, 4, 5, 6]]}


def decimal_round(expression, places=2):
    # MongoDB $round uses ties-to-even; use Decimal128 and floor(x*scale + 0.5).
    scale = 10 ** places
    return {"$cond": [{"$eq": [expression, None]}, None,
            {"$toDouble": {"$divide": [{"$floor": {"$add": [
                {"$multiply": [{"$toDecimal": expression}, scale]}, Decimal128("0.5")]}}, scale]}}]}


def presence_weight(weekdays_only=False):
    condition = {"$in": ["$status", PRESENCE]}
    if weekdays_only:
        condition = {"$and": [condition, weekday(mongo_day("$date"))]}
    return {"$cond": [condition, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}


def log_totals():
    return {"present_days": {"$sum": presence_weight(True)},
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}}}


def monthly_plan(emp_code, month):
    first, last = month_bounds(month)
    # Start with employees so a person with no logs still produces one summary.
    days = (Date.fromisoformat(last) - Date.fromisoformat(first)).days + 1
    pipeline = [{"$match": {"emp_code": emp_code}},
        {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [{"$match": {"date": {"$gte": first, "$lte": last}}},
                         {"$group": {"_id": None, **log_totals()}}], "as": "totals"}},
        {"$set": {"totals": {"$ifNull": [{"$arrayElemAt": ["$totals", 0]}, {}]},
            "working_days": {"$size": {"$filter": {"input": {"$range": [0, days]}, "as": "offset",
                "cond": {"$let": {"vars": {"day": {"$dateAdd": {"startDate": mongo_day(first),
                    "unit": "day", "amount": "$$offset"}}}, "in": {"$and": [weekday("$$day"),
                    {"$gte": ["$$day", mongo_day("$joined_on")]}]}}}}}}}},
        {"$project": {"_id": 0, "emp_code": 1, "month": {"$literal": month}, "working_days": 1,
            "present_days": {"$ifNull": ["$totals.present_days", 0]},
            "leave_days": {"$ifNull": ["$totals.leave_count", 0]},
            **{k: {"$ifNull": ["$totals." + k, 0]} for k in
               ("late_count", "total_late_minutes", "total_overtime_minutes")}}},
        {"$set": {"attendance_pct": {"$cond": [{"$eq": ["$working_days", 0]}, None,
            decimal_round({"$multiply": [{"$divide": [{"$toDecimal": "$present_days"}, "$working_days"]}, 100]})]}}}]
    return "employees", pipeline, "employee_code"


def summary_plan(month, department=None):
    first, last = month_bounds(month)
    match = {"joined_on": {"$lte": last}}
    if department is not None:
        match["department"] = department
    work = {"$cond": [{"$and": [{"$in": ["$status", PRESENCE]},
                {"$ne": [{"$ifNull": ["$work_hours", None]}, None]}]}, {"$toDecimal": "$work_hours"}, None]}
    totals = log_totals()
    totals.update(work_sum={"$sum": work}, work_count={"$sum": {"$cond": [{"$ne": [work, None]}, 1, 0]}})
    keys = ("present_days", "late_count", "total_late_minutes", "leave_count", "on_duty_count", "work_sum", "work_count")
    pipeline = [{"$match": match},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}, "emp_codes": {"$push": "$emp_code"}}},
        {"$lookup": {"from": "attendance_logs", "localField": "emp_codes", "foreignField": "emp_code",
            "pipeline": [{"$match": {"date": {"$gte": first, "$lte": last}}},
                         {"$group": {"_id": None, **totals}}], "as": "totals"}},
        {"$set": {"totals": {"$arrayElemAt": ["$totals", 0]}}},
        {"$set": {k: {"$ifNull": ["$totals." + k, 0]} for k in keys}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1,
            **{k: 1 for k in keys if not k.startswith("work_")},
            "avg_work_hours": {"$cond": [{"$eq": ["$work_count", 0]}, None,
                decimal_round({"$divide": ["$work_sum", "$work_count"]})]}}},
        {"$sort": {"department": 1}}]
    return "employees", pipeline, "employee_department" if department is not None else "employee_joined"


def leaderboard_plan(month, limit=10, department=None):
    first, last = month_bounds(month)
    pipeline = [{"$match": {"date": {"$gte": first, "$lte": last}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "late_count": log_totals()["late_count"]}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"}]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {"_id": 0, "emp_code": "$_id", "name": "$employee.name",
                      "department": "$employee.department", "total_late_minutes": 1, "late_count": 1, "rank": 1}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}}])
    return "attendance_logs", pipeline, "attendance_date"


def validate_range(first, last):
    days = (Date.fromisoformat(last) - Date.fromisoformat(first)).days + 1
    if days < 1 or days > 92:
        invalid("Range must contain 1 to 92 calendar days")
    return days


def trend_plan(department, first, last):
    days = validate_range(first, last)
    # Generate a bounded calendar in MongoDB. Unlike densifying only logs, this
    # also works for a department with no attendance anywhere in the interval.
    pipeline = [{"$match": {"department": department}},
        {"$group": {"_id": None, "join_dates": {"$push": "$joined_on"}, "emp_codes": {"$push": "$emp_code"}}},
        {"$lookup": {"from": "attendance_logs", "localField": "emp_codes", "foreignField": "emp_code",
            "pipeline": [{"$match": {"date": {"$gte": first, "$lte": last}}},
                         {"$group": {"_id": "$date", "present_count": {"$sum": presence_weight()},
                                     "late_count": log_totals()["late_count"]}}], "as": "daily"}},
        {"$set": {"offset": {"$range": [0, days]}}}, {"$unwind": "$offset"},
        {"$set": {"day": {"$dateAdd": {"startDate": mongo_day(first), "unit": "day", "amount": "$offset"}}}},
        {"$set": {"date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d", "timezone": "+05:30"}},
                  "is_working_day": weekday("$day")}},
        {"$set": {"headcount": {"$size": {"$filter": {"input": "$join_dates", "as": "joined",
                                                        "cond": {"$lte": ["$$joined", "$date"]}}}}}},
        {"$set": {"totals": {"$arrayElemAt": [{"$filter": {"input": "$daily", "as": "row",
                                                          "cond": {"$eq": ["$$row._id", "$date"]}}}, 0]}}},
        {"$set": {"present_count": {"$ifNull": ["$totals.present_count", 0]},
                  "late_count": {"$ifNull": ["$totals.late_count", 0]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                    decimal_round({"$divide": [{"$toDecimal": "$present_count"}, "$headcount"]}, 4), None]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_7d": {
            "$avg": {"$toDecimal": "$attendance_rate"}, "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
                      "present_count": 1, "late_count": 1, "attendance_rate": 1,
                      "moving_avg_7d": decimal_round("$moving_avg_7d", 4)}}, {"$sort": {"date": 1}}]
    return "employees", pipeline, "employee_department"


def aggregate(plan):
    collection, pipeline, hint = plan
    return json_value(list(db[collection].aggregate(pipeline, hint=hint, allowDiskUse=True)))


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Month):
    items = aggregate(monthly_plan(emp_code, month))
    if not items:
        raise HTTPException(404, "Unknown employee")
    return items[0]


@app.get("/analytics/departments/summary")
def department_summary(month: Month, department: str | None = None):
    return {"month": month, "items": aggregate(summary_plan(month, department))}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: Month, limit: Limit = 10, department: str | None = None):
    return {"month": month, "items": aggregate(leaderboard_plan(month, limit, department))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: Annotated[Day, Query(alias="from")], to: Day):
    plan = trend_plan(department, from_, to)
    items = aggregate(plan)
    if not items:
        raise HTTPException(404, "Unknown department")
    return {"department": department, "items": items}


ExplainEndpoint = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: ExplainEndpoint, emp_code: str | None = None, month: Month | None = None,
                     department: str | None = None, limit: Limit = 10, date_from: Day | None = None,
                     date_to: Day | None = None, status: Status | None = None,
                     from_: Annotated[Day | None, Query(alias="from")] = None, to: Day | None = None,
                     page: Page = 1, page_size: PageSize = 20):
    if endpoint == "attendance_list":
        query, hint = attendance_query(emp_code, date_from, date_to, status)
        collection = "attendance_logs"
        command = {"find": collection, "filter": query, "sort": {"date": -1, "emp_code": 1},
                   "skip": (page - 1) * page_size, "limit": page_size, "hint": hint}
    else:
        if endpoint == "department_trend":
            if department is None or from_ is None or to is None:
                invalid("department, from and to are required")
            plan = trend_plan(department, from_, to)
        else:
            if month is None:
                invalid("month is required")
            if endpoint == "employee_monthly":
                if emp_code is None:
                    invalid("emp_code is required")
                plan = monthly_plan(emp_code, month)
            elif endpoint == "department_summary":
                plan = summary_plan(month, department)
            else:
                plan = leaderboard_plan(month, limit, department)
        collection, pipeline, hint = plan
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}, "hint": hint, "allowDiskUse": True}
    return {"endpoint": endpoint, "collection": collection,
            "explain": json_value(db.command("explain", command, verbosity="executionStats"))}
