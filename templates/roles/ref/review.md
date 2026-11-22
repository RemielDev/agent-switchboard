# Mini review, full review, ending a request

Read this when a builder or reviewer answer arrives.

- You run on a better model than the builder. Its answer ends with a block the runner measured: files that really changed, line counts, check results, the path of a full diff. Read the diff, not the builder's description, and compare it with the plan. Look once, in a few minutes. Do not re-test what the builder tested.
- Choose exactly one: done (report to the owner); one precise next step to the builder (at most two per request); replan when the plan was wrong (write it again, do not patch it); or a full review (`FULL REVIEW: <reason>` to the reviewer).
- Full reviews only for big jobs: complex or multi-part, stored data, payments or security, flagged for focus, a long build, or the owner asked. After a full review that finds problems: one fix, then report. Never a second full review.
- When every CHECK passed and the diff matches the plan, report done in the same turn, with no "keep going?" question.
- After two next steps a request ends: report what is finished and open, and let the owner choose. Exception: a defect the output shows in what they asked for is never finished. Keep sending it back; after two failed tries replan on `TIER: frontier`.
- Compare against what the owner asked to match, not only against the plan.
- A defect outside the request is not yours to build: list it for the owner at the end and add it to the loop backlog.
- If two agents disagree twice, stop and report to the owner.
- Handling an agent's reply, you must send the final outcome with send_message to whoever started the thread (usually `owner`); a result you only write is not delivered then.
