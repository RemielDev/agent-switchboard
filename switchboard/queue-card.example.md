---
repo: Kepler
machine: any            # any | Desk-PC | Laptop-Mac
budget_minutes: 120
permission_mode: auto   # auto | acceptEdits | plan
---
# Fix the date parser on two-digit years

Goal: dates like 03/04/25 parse as 2025, not 1925, everywhere the importer reads them.
Done when: the fix is in source, a unit test covers two-digit years and passes, and a short report lists what
changed and what was not tested. Do not deploy.
