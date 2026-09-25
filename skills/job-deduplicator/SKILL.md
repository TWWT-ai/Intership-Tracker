---
name: job-deduplicator
description: >
  Decide whether two listings are the same internship. Use when the same role
  appears on several platforms, when the workbook seems to hold duplicate rows,
  when two jobs were merged that should not have been, or when tuning the
  matching thresholds.
---

# Job deduplicator

One row per real job, with every platform it was seen on. Implementation:
`tracker/dedupe.py`, inspect with `python -m scripts.deduplicate_jobs`.

## Why conservatism wins

The two failure modes are not equal.

A **duplicate row** is visible. The user sees two NVIDIA rows, is mildly
annoyed, and still applies.

A **wrong merge** is invisible. Two different internships become one row, and
the user never learns the second one existed. There is no way to notice the
absence.

So when in doubt, do not merge.

## The comparison

Blocking on normalized company first, then within that:

0. **Listing id** — two listings from the same platform with different
   `external_id`s are two postings, full stop. Trackr lists each season's round
   of a programme under its own id ("GIC Internship Programme 2026" and "...
   2027"), and those normalize to the same title.
1. **Title similarity** — `rapidfuzz.token_set_ratio` on normalized titles,
   threshold 88. Normalization has already expanded `SWE` to `software
   engineer`, converged `engineering`/`engineer`, and stripped the packaging
   words (`Intern`, `Summer 2027`, `12 month`, the city).
2. **Location** — both stated and different means different postings.
3. **Deadline** — more than 21 days apart means different intakes, e.g. a 2026
   and a 2027 round listed side by side.
4. **Work type** — a summer internship is not a 12-month placement.
5. **Title years** — normalization strips years, so `title_years()` reads them
   back from the raw title. Both titles naming years, and none in common, means
   different intakes. An undated title never blocks.

Any one of these failing blocks the merge.

## Merging

- **Sources accumulate.** Every `SourceRef` survives, with its own URLs. This is
  what lets the workbook show one row and still link to all three platforms.
- **Something beats nothing.** For each empty field, take the other listing's
  value. Neither platform is authoritative.
- **Skills combine.** Each platform lists a partial set.
- **Earliest deadline wins.** If platforms disagree, the later date risks
  missing the real cutoff.
- **Primary source** is whichever gives most (a real deadline *and* an
  application URL), with `primary_source_preference` in `config/sources.yaml`
  breaking ties.

## Stable IDs

The workbook matches the user's status and notes back to rows by ID, so:

- **One ID per row, every run.** Two rows sharing an ID means a status typed on
  one appears on the other.
- **An ID follows the listing.** The registry maps `ref:<source>:<external_id>`
  to an ID and that is looked up first. The fingerprint (company + title +
  location, nothing that moves) is the fallback that carries an ID across a
  retitle or onto a new platform.
- **The fingerprint never hands over an ID already bound to a different
  listing on the same platform.** The 2026 and 2027 rounds share a
  fingerprint; if the fallback could reuse IDs, the round the user applied to
  dropping off Trackr for a day would give its `APPLIED` to the other one.
- IDs minted before listings were keyed by platform ID may be contested by
  several rounds at once. The latest round wins (title year, then Trackr's
  oldest-first sweep order), because a status on the old merged row was set
  against the round that was open.

State from the last run (`last_run.json`) carries onto a job only when
`is_same_listing()` agrees, and only page-verified deadlines carry at all.

`data/state/id_registry.json` is load-bearing. If it is lost, every row is
renumbered and the workbook's history detaches. A corrupt registry raises rather
than silently restarting the numbering.

## When asked to loosen the threshold

Ask what was missed. Usually the fix is an entry in `company_aliases` or
`title_expansions` in `config/aliases.yaml`, not a lower threshold — aliases fix
one case precisely, a lower threshold risks every case.
