1. **Indexes.** Unique emp_code and employee/date indexes enforce identity and duplicate attendance. Department/code and joined_on/department support employee lists and headcount. Date/code and status/date/code support attendance pages and month scans; employee/punch_in finds the latest applicable punch. I rejected a late_minutes-only index because ranking first needs a bounded month and grouped employee totals.

2. **Punch-in race.** Both requests resolve the employee and compute the same attendance day. Both attempt an insert without a preliminary attendance check. MongoDB's unique index admits one insert; the other raises DuplicateKeyError, which becomes 409. Exactly one gets 201.

3. **Ties.** MongoDB's $rank computes competition ranks on total lateness alone. Only afterward do we select rank <= limit and order tied rows by emp_code. The cutoff can return more rows than limit.

4. **Headcount.** Summary starts from eligible employees, then looks up monthly log aggregates. Every employee increments headcount, even when the lookup is empty. Work sums and counts preserve record-weighted averages.

5. **100x data.** I would measure real query plans, then maintain incremental daily department rollups with a correction-aware rebuild path. That reduces repeated attendance scans while retaining raw records and audit history.
