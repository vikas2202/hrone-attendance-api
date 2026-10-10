The supplied assignment is a backend service, with no frontend requirement. The implementation follows the supplied OpenAPI v2 contract and MongoDB data model. Document contents were treated as project requirements; they do not authorize unrelated actions or changes to GitHub accounts.

Deliverables: `app/main.py`, root requirements, README, starter review, and five design decisions. All runtime application code belongs in that single Python file. Tests and explanatory documentation are separate. MongoDB 6.0+ is required; the evaluation environment uses 7.0.

The starter provides employee creation/listing, punch-in, attendance listing, and readiness. These require repairs for validation, UTC/IST conversions, pagination, serialization, indexes, and concurrency. The remaining work is punch-out, manual corrections, four analytics endpoints, and execution-statistics explain output.

Acceptance criteria:

- Exact endpoint paths, parameter names, response fields and error status codes.
- Strict integer epoch milliseconds, seconds truncation, IST attendance dates, overnight shifts, grace and overtime boundaries, decimal half-up rounding, and rounded-hours half-day decisions.
- Database uniqueness for employee and attendance keys; compare-and-swap updates with atomic correction-history appends.
- Database aggregation of stored derived values, weekday presence, employment-adjusted working days, headcount including employees without logs, record-weighted averages, competition ranks, and calendar-filled moving averages.
- Index-backed list/analytics queries, shared query construction for explain, automatic startup indexes, environment configuration, and no external service calls from the application.
- No committed credentials, `.env`, data dumps, virtual environment, or Dockerfile.

The supplied document permits AI as a reference and states that the candidate must understand every line and explain it in a live walkthrough. This repository includes tests and design notes to support that review; the candidate should read and understand the implementation before submitting it.
