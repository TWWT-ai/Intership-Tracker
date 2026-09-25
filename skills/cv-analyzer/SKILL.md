---
name: cv-analyzer
description: >
  Extract a structured candidate profile from a CV so job matching has real
  evidence behind it. Use when the user asks to set up or refresh
  data/profile.json, says their CV has changed, asks why a match score is low,
  or when a scoring run reports that the profile is not configured.
---

# CV analyzer

Turns a CV into `data/profile.json`. The implementation is
`scripts/analyze_cv.py`; this file is the judgement that goes around it.

## Run it

```bash
pipenv run python main.py profile --from-cv "D:\Claude\CoWork\Projects\CV Optimizer\library"
```

Reads `.tex`, `.md` and `.txt`. Writes `data/profile.draft.json` and **never**
overwrites `data/profile.json`. The user reviews the draft, corrects it, copies
it across and sets `profile_configured: true`.

## The one rule that matters

**A job requirement is never a candidate skill.**

A posting saying "CUDA required" tells you what the employer wants. It tells you
nothing about the candidate. If the CV does not mention CUDA, it does not go in
the profile — it belongs in `missing_skills` on that job instead.

The same applies to inference. A CV mentioning PyTorch does not imply CUDA,
Python does not imply pandas, and React does not imply TypeScript. Only what the
text actually says.

## Evidence levels

Every skill carries an evidence level, and only the top three count fully when
scoring:

| Level | Meaning |
|---|---|
| `professional` | Used in paid work or an internship |
| `demonstrated` | Used in something concrete and verifiable |
| `project` | Used in a named project on the CV |
| `coursework` | Studied, but no built artefact |
| `preferred` | Wants to work with it; no evidence yet |
| `uncertain` | Ambiguous — flag it, do not silently promote it |

`coursework` scores at half weight. `preferred` and `uncertain` score zero. That
gradient is the point: it lets the profile be honest about depth instead of
flattening everything into "I know it".

The script assigns `project` when a skill appears in a projects or experience
section and `coursework` when it appears only in a skills list. Both are
starting guesses for the user to correct — say so when you hand over the draft.

## Extract

Languages, frameworks, libraries, systems, hardware, ML, coursework, projects,
experience, achievements, domain knowledge, education, graduation year, current
year of study.

## Leave blank rather than guess

`current_year` is almost never stated unambiguously. Graduation year can be
misread from a date range. Project technologies are often described in prose the
parser cannot attribute safely.

Put these in `_uncertain` and ask. An empty field costs one question; a wrong
one silently distorts every score from then on.

## Before finishing

- Every skill traceable to a line in the CV
- No skill copied in from a job description
- `_uncertain` lists everything that needs a human
- `profile_configured` still `false` — only the user flips that
