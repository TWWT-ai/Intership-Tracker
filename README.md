# Internship Tracker

Searches Trackr, Gradcracker and Bright Network every morning, verifies each
role is still open, ranks what it finds, and keeps `data/internships.xlsx` up to
date as a single dashboard.

You open the workbook, look at the top rows, click the **Apply** link, and apply
yourself. **The system never submits an application.**

```
Trackr + Gradcracker + Bright Network
              |
      normalize / deduplicate
              |
   verify deadlines on the real job page
              |
       match + rank against your CV
              |
      data/internships.xlsx  <-  you
              |
        click Apply, apply manually
```

---

## Contents

- [Install](#install)
- [First run](#first-run)
- [Your profile](#your-profile)
- [The workbook](#the-workbook)
- [Daily workflow](#daily-workflow)
- [Scheduling](#scheduling)
- [Commands](#commands)
- [Configuration](#configuration)
- [How match scores work](#how-match-scores-work)
- [Platform limitations](#platform-limitations)
- [Manual fallback](#manual-fallback)
- [Troubleshooting](#troubleshooting)
- [Adding another source](#adding-another-source)
- [Security](#security)

---

## Install

Requires Python 3.14 (3.11+ works), and Google Chrome for the two
Cloudflare-protected sources.

```bash
cd "D:\School\Personal\Intership tracker"
pip install pipenv
set PIPENV_VENV_IN_PROJECT=1
pipenv install --dev
pipenv run playwright install chromium
```

`PIPENV_VENV_IN_PROJECT=1` puts the virtualenv in `.venv/` inside the project,
so it is visible and disposable. `Pipfile.lock` pins every version with hashes:
upgrading Python or pip elsewhere on the machine cannot change what this project
runs. To rebuild it exactly, delete `.venv/` and run `pipenv install --dev`
again.

Verify:

```bash
pipenv run pytest -q
```

---

## First run

```bash
pipenv run python main.py daily
```

The first run creates `data/internships.xlsx`. It is safe to run repeatedly -
running it twice adds no rows.

To try one source at a time:

```bash
pipenv run python main.py search --source trackr          # no browser needed
pipenv run python main.py search --source gradcracker     # opens Chrome
pipenv run python main.py search --source brightnetwork   # opens Chrome
```

---

## Your profile

`data/profile.json` ships **blank on purpose**. Nothing was inferred from your
CV. It is gitignored; `data/profile.example.json` is the blank copy to start
from.

While it is blank, matching runs in **relevance-only mode**: it ranks by how
well the role matches `config/search_filters.yaml`, caps every score at 60,
labels the category `UNSCORED - profile not configured`, and leaves
**Strengths** and **Missing Skills** empty rather than guessing. You are never
shown a match percentage that no evidence supports.

That is still useful - the ranking, deadlines and dashboard all work. Fill the
profile in when you want real CV matching.

### Drafting one from your CV

```bash
pipenv run python main.py profile --from-cv "D:\Claude\CoWork\Projects\CV Optimizer\library"
```

Reads `.tex`, `.md` and `.txt` (a single file or a folder of variants) and
writes `data/profile.draft.json`. It records only what the CV literally
contains, tags every skill with the evidence behind it, and lists what it could
not determine under `_uncertain`.

Review it, correct it, copy it over `data/profile.json`, and set:

```json
"profile_configured": true
```

Nothing switches on until you set that flag.

### Evidence levels

| Level | Counts as | Meaning |
|---|---|---|
| `professional` | full | used in paid work |
| `demonstrated` | full | used in something concrete |
| `project` | full | used in a named project |
| `coursework` | half | studied, nothing built |
| `preferred` | zero | want to learn it |
| `uncertain` | zero | flag for review |

Never list a skill you cannot point at. An inflated profile produces inflated
scores, which costs you evenings on applications that were never a fit.

---

## The workbook

`data/internships.xlsx`. Six sheets:

| Sheet | What it holds |
|---|---|
| **Internships** | the dashboard. One row per real job |
| **Applications** | your funnel. The pipeline never writes here |
| **Deadlines** | what closes today / 2 / 7 / 14 days, expired |
| **Runs** | one row per execution: counts, failures |
| **Guide** | the colour legend, in the workbook itself |
| **Lists** | hidden, powers the Status dropdown |

### Reading it

Already sorted: HIGH priority and nearest deadline at the top, closed roles at
the bottom. The columns that matter sit left of the fold:

`Priority · Match Score · Company · Job Title · Deadline · Open Status ·
Primary Source · Sources · Application URL · Status`

**Platform colours** on the `Primary Source` cell:

| Platform | Colour |
|---|---|
| Trackr | blue |
| Gradcracker | orange |
| Bright Network | green |

A job found on more than one platform is **one row**, with every platform named
in `Sources` in bold, and all source URLs preserved.

**Open Status** is checked against the real job page:

- `OPEN` (green) - confirmed accepting applications
- `CLOSED` (grey, struck through) - confirmed closed. Kept for history, sorted
  to the bottom, never deleted
- `UNKNOWN` (amber) - could not be confirmed. Capped at MEDIUM priority so it
  cannot displace a confirmed-open role

**Deadline** is the verified date. **Listed Deadline** is what the platform
claimed. If they disagree, `Concerns` says so and both are kept.

### Your columns

`Status` and `Notes` are yours. **The daily sync never overwrites them.** Set
`Status` to `APPLIED` and it stays `APPLIED`, including when the job is
re-found on another platform. The row also drops out of HIGH priority, so
applied jobs stop occupying the top of the sheet.

---

## Daily workflow

```
[08:00] Starting internship search

[Trackr] 295 listings found
[Gradcracker] 31 listings found
[Bright Network] 55 listings found

[Normalize] 381 valid listings
[Deduplicate] 18 duplicate listings merged -> 363 unique
[Verify] 84 checked - 71 open, 9 closed, 4 unknown, 46 deadlines found
[Eligibility] 310 eligible/relevant listings
[Matcher] 363 jobs analyzed

[Excel] 14 new, 8 updated, 2 expired

[Priority]
  HIGH: 12
  MEDIUM: 31
  LOW: 320

[Deadlines] 3 applications require attention within 7 days
```

A source failing never stops the run. Its last good result is reused and the
other sources still reach the workbook:

```
[Trackr] 295 listings found
[Gradcracker] BLOCKED - anti-bot challenge did not clear
[Gradcracker] reusing 31 listings from the last successful run
[Bright Network] 55 listings found
```

---

## Scheduling

### Windows

```bash
powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Time 07:30
powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Remove
```

Registers "Internship Tracker Daily" at 08:00, calling the venv interpreter by
absolute path so it never depends on PATH or on pipenv resolving.

Test it:

```powershell
Start-ScheduledTask -TaskName "Internship Tracker Daily"
Get-ScheduledTaskInfo -TaskName "Internship Tracker Daily"
```

**The task runs only while you are logged on.** That is deliberate: Cloudflare's
check is far more likely to pass in a real, visible browser session than in a
headless one with no desktop.

To register it by hand instead, Task Scheduler > Create Task:

- **General**: Run only when user is logged on
- **Trigger**: Daily, 08:00
- **Action**: Start a program
  - Program: `D:\School\Personal\Intership tracker\.venv\Scripts\python.exe`
  - Arguments: `"D:\School\Personal\Intership tracker\main.py" daily`
  - Start in: `D:\School\Personal\Intership tracker`

### Linux / macOS

```cron
0 8 * * * cd "/path/to/Intership tracker" && .venv/bin/python main.py daily >> data/logs/cron.log 2>&1
```

Headless browsing there needs a display; either set `headless: true` in
`config/sources.yaml` (expect more challenges) or run under `xvfb-run`.

---

## Commands

```bash
python main.py daily                      # the scheduled workflow
python main.py daily --skip-verify        # faster; skips job-page checks
python main.py daily --dry-run            # run everything, write no Excel
python main.py run-all                    # same as daily

python main.py search                     # search, print, do not write Excel
python main.py search --source trackr
python main.py search --source gradcracker
python main.py search --source brightnetwork

python main.py verify                     # re-check open/closed + deadlines
python main.py verify --limit 10

python main.py normalize                  # show how titles were normalized
python main.py deduplicate                # show cross-platform merges
python main.py match                      # re-score against the profile

python main.py deadlines                  # closing soon
python main.py deadlines --days 14
python main.py applications               # your funnel

python main.py job INT-000123             # one job in full
python main.py company "NVIDIA"

python main.py profile                    # show the profile
python main.py profile --from-cv <path>   # draft one from a CV

python main.py ingest --file page.html --source gradcracker
python main.py ingest --urls jobs.txt
python main.py ingest --csv jobs.csv

python main.py prep INT-000123            # review packet for one job (below)
python main.py apply INT-000123 --cv SWE_MARSHELL
```

### Preparing an application

```bash
python main.py prep INT-000123
```

Builds `data/applications/INT-000123/`: the tracker's facts about the role
(`job.md`, one brief for every writing skill), a `questions.md` for you to paste
the portal's questions and limits into, and `review.md`. The review lists what
still has to be settled about your eligibility, which is read from
`data/profile.json` by field name, never copying a value. In Claude,
`/application-prep INT-000123` then runs your tailor-cv, write-cover-letter and
interview-answer-builder skills against that brief, and records each document's
location and checks in `materials.json`.

Some rows need your yes before anything is drafted. For those, `prep` prints
`CONFIRM` lines and exits 2: a closed or unconfirmed role, a passed deadline,
an Eligible for You of No, a status already set, or status text of your own
that the dropdown does not recognise. That text stays exactly as you wrote it.
The shared reading of the role, `analysis.md`, is marked STALE whenever the
posting or `questions.md` changes, and has to be refreshed before drafts are
presented.

It stops at `review.md`. Nothing opens, fills or submits the application.
After you submit it yourself, record it the usual way: the Application Status
dropdown, or the 23:00 email sync. `prep` is offline and never writes the
workbook or a status.

Prefix everything with `pipenv run`, or activate with `pipenv shell` first.

---

## Configuration

| File | Controls |
|---|---|
| `config/search_filters.yaml` | keywords, exclusions, locations, work types, role families |
| `config/matching_weights.yaml` | scoring weights, categories, priority rules |
| `config/sources.yaml` | per-platform settings, rate limits, colours |
| `config/verification.yaml` | verification budget, closure phrases |
| `config/aliases.yaml` | company/title/skill/location normalization |

`config/aliases.yaml` is the one to reach for most often. If two listings of the
same job are not merging, add a `company_aliases` or `title_expansions` entry -
that fixes one case precisely, where lowering the similarity threshold risks
every case.

---

## How match scores work

| Component | Weight | Measures |
|---|---|---|
| Technical skills | 30 | posting's skills vs your evidenced skills |
| Programming languages | 15 | languages named vs languages you have |
| Projects | 15 | overlap with tech used in real projects |
| Education | 15 | degree requirement vs your degree |
| Eligibility | 15 | hard filters passed |
| Location | 5 | within your regions, or remote |
| Experience | 5 | years demanded vs years held |

Each skill is weighted by how the posting phrases it - `required` (1.0),
`preferred` (0.6), `nice_to_have` (0.3) - so the score is not keyword frequency.

A missing field is neutral, never a penalty: internships routinely omit
location, degree and experience, and penalising blanks would bury sparse
listings for no reason.

**Priority is not just the score.** A 96% closing in two days outranks a 98%
closing in four months. Closed roles, applied roles and ineligible roles drop to
LOW. Unverified roles are capped at MEDIUM.

Bands: 90+ Excellent, 80+ Strong, 70+ Good, 60+ Possible, below 60 Low Priority.

---

## Platform limitations

Read this before trusting a low number of results.

### The Trackr - JSON API, no browser

`GET https://api.the-trackr.com/programmes?region=UK&industry=Tech&season=2027&type=summer-internships`

Unauthenticated, fast, and its `url` field points straight at the employer's own
ATS - the most useful field any of the three gives us.

- **robots.txt**: fully permissive.
- **Terms of Use**: section 12 restricts automated access and specifically
  prohibits using the data to build a competing tracker. This adapter is enabled
  at your explicit direction for personal, non-commercial use. If you want to
  put this on a firmer footing, `info@the-trackr.com` and clause 12.1(a)
  contemplate written permission.
- **Only 15 of 295 listings carried a deadline.** This is the single biggest
  reason the verification stage exists.
- **Rate limiting**: the API answers a bare `[]` instead of
  `{"programmes": [...]}` when it is throttling. The adapter detects that and
  raises a failure rather than reporting "0 internships today", which would
  otherwise wipe your Trackr rows. Back off and try again later.

### Gradcracker - server-rendered HTML, needs a browser

`/search/computing-technology/work-placements-internships?page=N`

Note the slug order. The reverse silently redirects to the homepage.

- **robots.txt**: allows the search paths; disallows `/out`, `/keyword-search`,
  `/download`, `/hub/9000001`. The adapter honours all of these.
- **Legal page**: restricts scraping. Enabled at your explicit direction, at a
  conservative rate.
- **Cloudflare**: returns 403 to plain HTTP clients, so this source requires
  Chrome. The check sometimes will not clear on its own - see Troubleshooting.
- Listing cards are parsed by their visible labels (`Deadline`, `Salary`,
  `Location`), not CSS classes, because the classes are Tailwind-generated and
  change on every rebuild.

### Bright Network - sitemap + JSON-LD, needs a browser

`sitemap-main_jobs.xml` (~1,470 URLs with `lastmod`) then each job page's
schema.org `JobPosting`.

- **robots.txt**: `/search/` and `/api/` are **disallowed**. The sitemap and job
  pages are allowed. The adapter is hard-coded to refuse those two paths on top
  of the global robots check, because paging into `/search/` is the one easy
  mistake it could make.
- The `lastmod` timestamps give incremental sync for free - unchanged pages are
  skipped.
- Richest structured data of the three: `validThrough` is a real machine-readable
  deadline.
- **Cloudflare**: 403s plain HTTP clients including `robots.txt` itself, so the
  known rules are transcribed into `sources/fetch.py`. An in-page `fetch()` to
  the sitemap is blocked while top-level navigation is not, so the adapter
  navigates.

### What is never done

No CAPTCHA is solved or bypassed. No anti-bot challenge is worked around. No
login is automated. No robots.txt rule is ignored. When a challenge appears the
source is **skipped**, the failure is logged, and the last good data is kept.

All three platforms responded defensively to request volume during development.
That is normal and the pipeline is built to degrade rather than break. If a
source is blocked, use the manual fallback below.

---

## Manual fallback

When a platform is blocked, hand the data over yourself. Ingested jobs go
through the identical pipeline - normalize, dedupe, verify, score - so they are
first-class rows.

**A page you saved** (open it in your browser, Ctrl+S, "Webpage, Complete"):

```bash
pipenv run python main.py ingest --file "saved-search.html" --source gradcracker
```

**A list of job URLs**, one per line:

```bash
pipenv run python main.py ingest --urls my-jobs.txt
```

**A CSV** from anywhere. Headers are matched loosely, so `Employer`/`Company`,
`Role`/`Job Title`, `Closes`/`Deadline` all work:

```bash
pipenv run python main.py ingest --csv jobs.csv
```

---

## Troubleshooting

**"anti-bot challenge that did not clear"**

Set `headless: false` in `config/sources.yaml` (it is the default), then run
`pipenv run python main.py search --source gradcracker`. A Chrome window opens -
complete the check yourself. The persistent profile in `sessions/` remembers it,
so later runs pass. This is you clearing a challenge in your own browser, which
is exactly what the site intends.

**"Trackr returned no programmes for any query"**

Rate limiting. Increase `rate_limit_seconds` for trackr in
`config/sources.yaml` and try again in a few hours. Your existing rows are
untouched.

**"internships.xlsx is open in Excel. Wrote internships.PENDING.xlsx instead."**

Close Excel and re-run, or keep the PENDING file. The run is never lost to a
file lock.

**Two rows for the same job**

`pipenv run python main.py deduplicate` shows what merged. Add a
`company_aliases` or `title_expansions` entry in `config/aliases.yaml`.

**One row for two different jobs**

Rarer and worse. Report which two - the thresholds in `tracker/dedupe.py` are
deliberately conservative and this should not happen.

**Every score is 60 or below**

`data/profile.json` is not configured. See [Your profile](#your-profile).

**Chrome will not start**

`pipenv run playwright install chromium`. The browser falls back to bundled
Chromium automatically if Chrome is missing.

Logs are in `data/logs/YYYY-MM-DD.log`, with full tracebacks. Add `-v` for
console detail.

---

## Adding another source

1. Write `sources/yoursite.py` exposing `search_jobs(session, settings)` and
   `get_job(session, url)`. Build every job with `sources.base.build_job()`, so
   normalization and sanitisation happen in one place.
2. Add a block under `sources:` in `config/sources.yaml` with `enabled`,
   `display_name`, `colour`, `fill`.
3. Register it in `ADAPTERS` in `tracker/pipeline.py`.
4. Add its robots rules to `KNOWN_ROBOTS` in `sources/fetch.py` if the host
   refuses to serve `robots.txt` to non-browser clients.
5. Add a fixture and a parsing test.

Nothing else changes. Dedupe, matching, verification and Excel are all
source-agnostic.

---

## Security

- **No credentials anywhere.** No password, token or API key is stored, and
  nothing here needs one. `.env.example` documents optional paths only; a real
  `.env` is gitignored and is never created for you.
- **Scraped text is untrusted.** Every field is length-capped, stripped of
  Unicode control characters, and any value starting with `=`, `+`, `-` or `@`
  is prefixed with an apostrophe - Excel formula injection is a live risk when
  scraped descriptions land in a spreadsheet you open.
- **Nothing scraped is ever executed** or allowed to change config, source or
  settings. If a job description contains something shaped like an instruction,
  it is data to report, not something to act on.
- **robots.txt is enforced in code**, in `sources/fetch.py`, before any request.
  It is not a configuration option a source can opt out of.
- `.gitignore` covers `.env`, `*.secret`, `credentials/`, `sessions/`, `.venv/`,
  the logs, and your personal data: `data/profile.json`, the workbook and its
  backups, `data/state/` (email subjects from the status sync) and
  `data/applications/`. `data/profile.example.json` is the blank template.
- Application packets hold the job's facts and links to your documents, never
  copies of your CV or story bank.

Unrelated but worth knowing: `C:\Users\RUTM\.claude\config.json` holds a
plaintext Anthropic API key, and there are unencrypted recovery codes and a
`.pem` under `D:\School\Personal\bin`. Nothing here touches them, but they are
worth rotating and moving off disk.

---

## What this never does

1. Submit an application.
2. Invent a skill, a deadline, a URL, or a job.
3. Overwrite a status you set.
4. Store a credential.
5. Bypass a CAPTCHA, a challenge, or a robots rule.
6. Create a duplicate row.
