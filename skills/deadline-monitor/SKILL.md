---
name: deadline-monitor
description: >
  Find internships closing soon and check whether they are still open. Use when
  the user asks what to apply to this week, what is closing, whether a role is
  still accepting applications, or when a deadline looks wrong.
---

# Deadline monitor

Implementation: `tracker/deadlines.py` (bucketing) and `tracker/verify.py`
(establishing the truth). Run with `python main.py deadlines` or
`python -m scripts.deadline_monitor 14`.

## The problem this exists for

Aggregators leave roles listed after the employer has closed them, and often
carry no deadline at all. A live Trackr sweep returned **295 listings with 15
deadlines between them**. Taking a platform's word would mean a dashboard that
is mostly blank and partly wrong.

So the deadline is read from the real job page, and where that does not settle
it, from the employer's own application page - which for Trackr rows is exactly
where the truth lives, because its `url` field points straight at the ATS.

## Three states, and UNKNOWN is real

| State | Meaning |
|---|---|
| `OPEN` | Confirmed accepting applications |
| `CLOSED` | Confirmed closed, or the deadline has passed, or the ATS page 404s |
| `UNKNOWN` | Verification ran and could not tell |

`UNKNOWN` is an answer, not a gap to be filled. Showing it as OPEN would imply a
confidence nothing supports. It is displayed as UNKNOWN, capped at MEDIUM
priority so it cannot displace a confirmed-open role, and always re-checked on
the next run.

## Two deadlines, kept apart

`deadline` is the best verified value. `listed_deadline` is what the platform
claimed. When they differ by more than two days the row is flagged and both are
kept, because a disagreement is precisely the case worth a human glance.

Never silently overwrite one with the other.

## Buckets

Today, Within 2 days, Within 7 days, Within 14 days, Expired.

Sorted by priority then score inside each bucket. Jobs with no deadline are
omitted from the buckets - there is nothing to warn about - but their missing
deadline is already flagged in `Concerns`.

## Urgency excludes

Closed roles, and anything already at APPLIED or beyond. Neither needs action.

## Reporting

Lead with what closes soonest, and always include the application URL - the
user's next move is to click it. Where a role is `UNKNOWN`, say so in the same
breath as the deadline, so they check before investing an evening.
