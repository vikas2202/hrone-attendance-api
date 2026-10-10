# Starter review

Locations refer to functions in the original candidate-kit starter.

| # | Where | What is wrong | How to notice | Fix |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Uses the timestamp's local/UTC wall clock instead of the IST shift instant. | A 04:35 UTC punch for a 09:30 IST shift should be 35 minutes late. | Compare UTC-aware punch with IST shift start. |
| 2 | `compute_late_minutes` | Floors before testing the grace threshold. | 09:40:01 returns 0 instead of 10. | Test elapsed seconds > 600 before flooring minutes. |
| 3 | `compute_late_minutes` | No overnight attendance-date context. | 00:10 punch for a 22:00–06:00 shift belongs to the preceding day. | Compute R1 attendance day and shift bounds first. |
| 4 | `compute_work_hours` | Binary float / ties-to-even rounding. | 18 min 54 sec = 0.315 hours; expected 0.32. | Decimal division with ROUND_HALF_UP. |
| 5 | `compute_work_hours` | No duration validation in write flow. | Equal/earlier punch-out or 24h01s interval. | Reject non-positive or >24-hour duration before writing. |
| 6 | `compute_overtime` | Counts under 30 minutes. | 18:59:59 for an 18:30 end must return 0. | Floor minutes, then require >=30. |
| 7 | `compute_overtime` | Overnight end placed on the wrong day. | 06:40 next-day out for a night shift. | Add one calendar day to overnight end. |
| 8 | All models/queries | Missing constraints for codes, text lengths, email, shifts, dates, month, statuses and pages. | Invalid date, 25:00, ABSENT punch-in, page 0. | Typed validated request models and query parameters. |
| 9 | Timestamp input | Coerces strings/floats and accepts seconds or out-of-range values. | `"1783312500000"`, 1783312500, JSON null. | Strict bounded integer milliseconds; defaults only on omission. |
| 10 | Timestamp handling | Host-dependent naive datetimes. | Different host timezones give different attendance days. | UTC-aware PyMongo, explicit IST at calendar boundary. |
| 11 | Write helpers | Does not truncate instants before calculation/storage. | 09:40:00.999 must count as 09:40:00. | Integer milliseconds floor to whole seconds. |
| 12 | `health` | Reports ready without contacting MongoDB. | Stop MongoDB; still gets 200. | Ping database; return 503 on driver failure. |
| 13 | Startup | No indexes or uniqueness guarantees. | Parallel duplicate requests / large collection scans. | Idempotent unique and compound indexes at startup. |
| 14 | `create_employee` | Check-then-insert race. | Eight simultaneous identical employee creates. | Unique index, insert directly, map duplicate to 409. |
| 15 | `create_employee` | Server creation time is naive and returned as ISO text. | Response `created_at` is not an integer. | UTC timestamp and explicit millisecond serialization. |
| 16 | `list_employees` | Wrong skip: page 1 omits the first page. | First page on a two-employee collection. | `(page - 1) * page_size`. |
| 17 | `list_employees` | Total ignores department filter. | One department in a multi-department database. | Count the same query used by the page. |
| 18 | `list_employees` | No specified ordering. | Insert employees out of code order. | Sort emp_code ascending in MongoDB. |
| 19 | Filter builders | Truthiness drops exact empty-string filters. | `department=` incorrectly lists all employees. | Distinguish None from supplied strings. |
| 20 | `punch_in` | Missing employee produces an exception rather than 404. | Punch unknown emp_code. | Explicit missing-resource check. |
| 21 | `punch_in` | Check-then-insert duplicates attendance under concurrency. | Eight simultaneous same-day punches. | Unique natural key and duplicate-key 409. |
| 22 | `punch_in` | Uses host date; no R1 overnight exception. | Punch around IST midnight / night-shift end. | Explicit IST calendar and strictly-earlier-than-end rule. |
| 23 | `punch_in`, `list_attendance` | Exposes an `id` and returns BSON timestamps without boundary conversion. | Response contains `id` and ISO punch values. | Whitelisted response fields; recursive date-to-ms conversion. |
| 24 | `list_attendance` | Loads and sorts the entire filtered collection in Python. | 100,000 records, page_size=20. | Indexed server-side sort, skip, limit and filtered count. |
| 25 | `list_attendance` | Tie ordering lacks emp_code ascending. | Multiple employees on the same date. | Compound date-desc/code-asc sort. |
| 26 | `list_attendance` | Does not reject reversed/invalid date ranges. | date_from > date_to; February 30. | Real calendar validation and ordered-range check. |
| 27 | `list_attendance` | Does not normalize missing legacy history/half_day. | Seeded legacy record lacks both. | Defaults [] and false (also missing minute totals to zero). |

The missing endpoints were unfinished scope, not hidden helper defects. They are now implemented, including atomic punch-out and audited corrections. Analytics and explain share pipeline builders.

Reviewed and retained: `load_dotenv()` does not override existing environment variables by default; calendar strings are correct for this data model; optional query filters and read-only extra body fields are appropriate; work_hours remains null until punch-out. A local default MongoDB URI/database is allowed because injected environment variables take priority. The original work-hours duration formula itself is correct; its rounding and validation were defective.
