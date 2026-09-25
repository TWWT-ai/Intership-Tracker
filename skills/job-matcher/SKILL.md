---
name: job-matcher
description: >
  Score an internship against the candidate profile and explain the number. Use
  when the user asks why a job scored what it did, wants scores recalculated,
  wants to change the weighting, or asks whether a role is worth applying to.
---

# Job matcher

Produces a 0-100 score with reasons. Implementation: `tracker/match.py`,
weights in `config/matching_weights.yaml`, inspect with
`python -m scripts.match_jobs`.

## Never claim what you cannot evidence

An inflated score is not harmless optimism. It sends the user to spend a real
evening on an application they were never a fit for, and it teaches them to
distrust every other number in the sheet.

So: a strength requires a profile entry with `professional`, `demonstrated` or
`project` evidence. Nothing else is a strength. If the profile is empty, the
answer is "unscored", not a guess.

## Blank-profile mode

While `profile_configured` is `false` the matcher scores **role relevance only**
- title and description against `config/search_filters.yaml`, plus location,
work type and eligibility. It caps the score at 60, labels the category
`UNSCORED - profile not configured`, and returns **empty** strengths and missing
skills.

This is deliberate. Ranking by relevance is genuinely useful on day one; ranking
against a CV that does not exist is fabrication. Never fill those lists in this
mode.

## Components

| Component | Weight |
|---|---|
| Technical skills | 30 |
| Programming languages | 15 |
| Projects | 15 |
| Education | 15 |
| Eligibility | 15 |
| Location | 5 |
| Experience | 5 |

## Requirements are not equal

Each skill the posting names is classified by the sentence it appears in:

- `required` (1.0) - "essential", "must have", "you will need"
- `preferred` (0.6) - "desirable", "ideally", "advantageous"
- `nice_to_have` (0.3) - "bonus", "exposure to", "familiarity with"

This is what stops the score being keyword frequency. "Rust is a bonus" and
"Rust is essential" must not cost the same to be missing.

## Absence of data is not failure

An unstated location, degree requirement or experience bar is neutral, never a
penalty. Internships routinely omit these. Penalising a blank field would push
sparse listings - which is most of Trackr - to the bottom for no reason.

## Categories

90-100 Excellent, 80-89 Strong, 70-79 Good, 60-69 Possible, 0-59 Low Priority.

## Explaining a score

Give the component breakdown, not just the total. "68 because Python and C++
matched but the posting requires CUDA, which your CV does not evidence" is
actionable. "68" is not.

If the profile is unconfigured, say so first - it explains the cap, and it is
usually the real answer to "why is this so low".
