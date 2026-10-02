"""Build the Algorithm Engineer, AI roles and Forward-Deployed Engineer sheets.

Every row is classified on what the posting says it involves, not on its title,
and its languages, tools and knowledge areas are taken from the posting's own
words - a technology common in the field but absent from the description is not
recorded. Match is shown per criterion; nothing is scored where the posting is
silent. Existing sheets are untouched: the workbook is backed up, the three
sheets are written, then the result is reloaded and verified before it replaces
the file.

Sources: data/state/last_run.json (the pipeline's own rows, with descriptions)
and data/checkpoint/target-roles.json (the target-area board sweep).
"""
import json
import os
import re
import shutil
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from openpyxl import load_workbook  # noqa: E402
from openpyxl.styles import Alignment, Font, PatternFill  # noqa: E402
from openpyxl.utils import get_column_letter  # noqa: E402

from tracker import config, normalize  # noqa: E402

WORKBOOK = ROOT / "data" / "internships.xlsx"
BACKUP = ROOT / "data" / "checkpoint" / "backups" / "internships-before-target-sheets.xlsx"
DRAFT = ROOT / "data" / "checkpoint" / "internships-target-draft.xlsx"
SWEEP = ROOT / "data" / "checkpoint" / "target-roles.json"
FDE_CSV = ROOT / "data" / "checkpoint" / "fde-roles.csv"
TODAY = date.today()

SHEETS = ["Algorithm Engineer", "AI roles", "Forward-Deployed Engineer"]
COLUMNS = ["COMPANY", "ROLE", "ROLE CATEGORY", "LOCATION", "WORK TYPE", "INTERNSHIP DURATION",
           "START DATE", "DATE POSTED", "APPLICATION DEADLINE", "ELIGIBILITY / YEAR REQUIREMENT",
           "PROGRAMMING LANGUAGES", "KEY TECHNICAL SKILLS", "KEY KNOWLEDGE AREAS",
           "DESCRIPTION / RESPONSIBILITIES", "SOURCE", "OFFICIAL APPLICATION URL",
           "EXTERNAL LISTING URL", "STATUS", "DATE FOUND", "NOTES",
           "MATCH: ROLE", "MATCH: LANGUAGES", "MATCH: KNOWLEDGE", "MATCH: ELIGIBILITY",
           "MATCH: LOCATION", "MATCH: TIMING"]
WIDTHS = {"COMPANY": 22, "ROLE": 44, "ROLE CATEGORY": 26, "LOCATION": 22, "WORK TYPE": 18,
          "INTERNSHIP DURATION": 16, "START DATE": 12, "DATE POSTED": 12, "APPLICATION DEADLINE": 14,
          "ELIGIBILITY / YEAR REQUIREMENT": 40, "PROGRAMMING LANGUAGES": 22,
          "KEY TECHNICAL SKILLS": 26, "KEY KNOWLEDGE AREAS": 30,
          "DESCRIPTION / RESPONSIBILITIES": 60, "SOURCE": 18, "OFFICIAL APPLICATION URL": 34,
          "EXTERNAL LISTING URL": 30, "STATUS": 14, "DATE FOUND": 11, "NOTES": 34,
          "MATCH: ROLE": 12, "MATCH: LANGUAGES": 14, "MATCH: KNOWLEDGE": 14,
          "MATCH: ELIGIBILITY": 15, "MATCH: LOCATION": 13, "MATCH: TIMING": 12}

FDE_EVIDENCE = "UNDERGRADUATE ELIGIBILITY EVIDENCE"
WIDTHS[FDE_EVIDENCE] = 50


def sheet_columns(name: str) -> list[str]:
    """Every target sheet shows why a current undergraduate may apply - the
    posting's own words where it states it - next to the eligibility column."""
    at = COLUMNS.index("ELIGIBILITY / YEAR REQUIREMENT") + 1
    return COLUMNS[:at] + [FDE_EVIDENCE] + COLUMNS[at:]


HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)
BANNER_FILL = PatternFill("solid", fgColor="D9D9D9")
MATCH_FILL = {"High": "C6EFCE", "Medium": "FFF2CC", "Low": "F8CBAD", "Verify": "FFEB9C"}

# ---------------------------------------------------------------- what the posting says


def words(*phrases):
    return re.compile(r"(?<![\w+#])(?:" + "|".join(p.replace(" ", r"\s+") for p in phrases) + r")(?![\w+#])",
                      re.IGNORECASE)


#: Languages are matched case-sensitively where the name is a single letter or a
#: common word ("C", "R", "Go"), so prose does not become a requirement.
LANGUAGES = [
    ("Python", re.compile(r"\bPython\b", re.I)), ("C++", re.compile(r"\bC\+\+")),
    ("C#", re.compile(r"\bC#")), ("C", re.compile(r"\bC\b(?!\+|#|\w)")),
    ("Java", re.compile(r"\bJava\b(?!Script)", re.I)), ("JavaScript", re.compile(r"\bJavaScript\b", re.I)),
    ("TypeScript", re.compile(r"\bTypeScript\b", re.I)), ("Go", re.compile(r"\bGolang\b|\bGo\b(?= programming| language)")),
    ("Rust", re.compile(r"\bRust\b")), ("MATLAB", re.compile(r"\bMATLAB\b", re.I)),
    ("SQL", re.compile(r"\bSQL\b", re.I)), ("R", re.compile(r"\bR\b(?= programming| language)|\bR/|\bR,")),
    ("Scala", re.compile(r"\bScala\b", re.I)), ("Kotlin", re.compile(r"\bKotlin\b", re.I)),
    ("Swift", re.compile(r"\bSwift\b")), ("Julia", re.compile(r"\bJulia\b")),
    ("Verilog", re.compile(r"\bVerilog\b", re.I)), ("VHDL", re.compile(r"\bVHDL\b", re.I)),
    ("Bash", re.compile(r"\bBash\b|\bshell scripting\b", re.I)),
]
TOOLS = ["pytorch", "tensorflow", "keras", "scikit-learn", "sklearn", "numpy", "pandas", "opencv", "ros",
         "ros2", "cuda", "jax", "hugging face", "huggingface", "langchain", "spark", "hadoop", "kafka",
         "airflow", "dbt", "docker", "kubernetes", "git", "github", "gitlab", "aws", "azure", "gcp",
         "google cloud", "linux", "jenkins", "terraform", "react", "node.js", "flask", "django",
         "fastapi", "simulink", "labview", "ansys", "solidworks", "snowflake", "databricks", "tableau",
         "power bi", "mlflow", "kubeflow", "sagemaker", "openai api", "rag"]
KNOWLEDGE = ["data structures", "algorithms", "algorithm design", "machine learning", "deep learning",
             "neural network", "neural networks", "computer vision", "image processing", "nlp",
             "natural language processing", "reinforcement learning", "generative ai", "large language model",
             "large language models", "llm", "llms", "linear algebra", "probability", "statistics",
             "optimisation", "optimization", "robotics", "path planning", "motion planning", "slam",
             "perception", "sensor fusion", "signal processing", "control systems", "numerical methods",
             "mathematical modelling", "mathematical modeling", "simulation", "distributed systems",
             "operating systems", "networking", "databases", "cloud computing", "embedded systems",
             "cryptography", "mlops", "data engineering", "software engineering", "object-oriented"]
TOOLS_RE = {t: words(t) for t in TOOLS}
KNOW_RE = {k: words(k) for k in KNOWLEDGE}

ALGO_STRONG = words("algorithm engineer", "algorithm development", "algorithm design", "algorithm developer",
                    "develop algorithms", "developing algorithms", "designing algorithms", "algorithm optimisation",
                    "algorithm optimization", "algorithmic development", "algorithms for")
ALGO_SIGNAL = words("algorithm", "algorithms", "mathematical modelling", "mathematical modeling",
                    "numerical methods", "path planning", "motion planning", "optimisation", "optimization",
                    "signal processing", "computational modelling", "computational modeling", "slam",
                    "sensor fusion", "perception", "computer vision", "control systems", "simulation")
FDE_TITLE = words("forward deployed engineer", "forward deployed software engineer",
                  "forward deployed infrastructure engineer", "forward deployed", "fde",
                  "deployment strategist")
FDE_SIGNAL = words("customer", "customers", "client", "clients", "deploy", "deployment", "integrate",
                   "integration", "prototype", "prototyping", "on-site", "onsite", "stakeholders",
                   "requirements", "real-world", "ambiguous")
AIML_STRONG = words("machine learning", "deep learning", "artificial intelligence", "generative ai",
                    "large language model", "large language models", "llm", "llms", "neural network",
                    "neural networks", "applied ai", "ai engineer", "ml engineer", "reinforcement learning")
AIML_SIGNAL = words("machine learning", "deep learning", "artificial intelligence", " ai ", "ml model",
                    "ml models", "ml pipeline", "models", "training data", "nlp",
                    "natural language processing", "computer vision", "data science", "mlops")
CV_SIGNAL = words("computer vision", "image processing", "object detection", "image segmentation",
                  "semantic segmentation", "instance segmentation", "image recognition",
                  "visual perception", "optical flow", "point cloud", "3d reconstruction")
ROBO_SIGNAL = words("robot", "robots", "robotics", "autonomy", "autonomous", "autonomous vehicle",
                    "self-driving", "ros", "drone", "uav", "motion planning", "slam")
SWE_TITLE = words("software engineer", "software engineering", "software developer", "developer")
NON_TECH_TITLE = words(*config.load("sources")["sources"]["careers"]["non_tech_function_terms"])
#: A title naming another field of study is that field's intake (the same list the
#: careers scan screens with): "Biological & Biomedical Sciences Placement" is not
#: an AI role because its lab work mentions automation and data.
OTHER_FIELD_TITLE = words(*config.load("sources")["sources"]["careers"]["other_field_terms"])
#: ...unless the title also names computing, as the careers screen already rules:
#: "GPU Internship - Platform Architecture" and "GE Aerospace Software Engineering"
#: name a field word but are computing roles.
TECH_TITLE = words(*config.load("sources")["sources"]["careers"]["tech_terms"])
STUDENT = words("intern", "interns", "internship", "internships", "placement", "placements",
                "year in industry", "summer analyst", "undergraduate", "student", "spring week",
                "insight", "industrial placement")
NEW_GRAD = words("new grad", "new graduate", "graduate programme", "graduate program", "graduate scheme")
SENIOR = words("senior", "staff", "principal", "lead", "manager", "director", "head", "vice president",
               "vp", "architect")
UK = words("united kingdom", "uk", "u.k.", "england", "scotland", "wales", "northern ireland", "london",
           "manchester", "cambridge", "oxford", "bristol", "edinburgh", "glasgow", "birmingham", "leeds",
           "reading", "sheffield", "nottingham", "cardiff", "belfast", "coventry", "warwick", "milton keynes",
           "newcastle", "liverpool", "southampton", "brighton", "derby", "bath", "york", "guildford",
           "basingstoke", "swindon", "preston", "warrington", "chester", "leicester", "farnborough")
NOT_UK = words("united states", "usa", "canada", "india", "singapore", "australia", "japan", "china",
               "germany", "france", "netherlands", "ireland", "poland", "brazil", "mexico", "new york",
               "san francisco", "seattle", "toronto", "bangalore", "tokyo", "paris", "berlin", "amsterdam",
               "dublin", "madrid", "zurich", "tel aviv", "sydney", "remote - us")


def found(text, table):
    return [name for name, pattern in table.items() if pattern.search(text)]


def languages_in(text):
    return [name for name, pattern in LANGUAGES if pattern.search(text)]


def count(pattern, text):
    return len(pattern.findall(text))


def categories(title: str, description: str) -> list[str]:
    """What the posting is, from its responsibilities. Title only breaks ties."""
    text = f"{title}. {description}"
    if NON_TECH_TITLE.search(title) or (OTHER_FIELD_TITLE.search(title) and not TECH_TITLE.search(title)):
        # "Finance & Accounting Intern", "Biological Sciences Placement": not a target area
        return []
    out = []
    if FDE_TITLE.search(title) or (count(FDE_SIGNAL, description) >= 6 and FDE_TITLE.search(text)):
        out.append("Forward Deployed Engineer")
    if ALGO_STRONG.search(text) or count(ALGO_SIGNAL, text) >= 3:
        out.append("Algorithm Engineer")
    if AIML_STRONG.search(title) or count(AIML_SIGNAL, text) >= 3:
        out.append("AI / ML")
    if CV_SIGNAL.search(text):
        out.append("Computer Vision")
    if count(ROBO_SIGNAL, text) >= 2:
        out.append("Robotics / Autonomy")
    if SWE_TITLE.search(title) and ("AI / ML" in out or AIML_STRONG.search(text)):
        out.append("AI Software Engineering")
    if not out and SWE_TITLE.search(title) and count(ALGO_SIGNAL, text) >= 1:
        out.append("Related Technical")
    return out


def level(title: str, work_type: str = "") -> str:
    if STUDENT.search(title) or STUDENT.search(work_type or ""):
        return "Internship / placement"
    if NEW_GRAD.search(title):
        return "Graduate"
    if SENIOR.search(title):
        return "Senior"
    return "Experienced"


def uk_ish(location: str) -> str:
    low = location or ""
    if UK.search(low):
        return "UK"
    if NOT_UK.search(low):
        return "Not UK"
    return "Not stated"


def trim(text: str, limit: int = 900) -> str:
    text = " ".join((text or "").split())
    return text[:limit] + ("..." if len(text) > limit else "")


# ---------------------------------------------------------------- rows
#: A location that names a country or a mode of work, not a place. The UK and
#: NOT_UK patterns cannot be used here: they list cities, and "London" must stay
#: a place - treating it as country-level merged London postings into Seoul's.
COUNTRY_ONLY = words("united kingdom", "uk", "u.k.", "great britain", "britain", "england", "scotland",
                     "wales", "northern ireland", "united states", "usa", "us", "united states of america",
                     "remote", "hybrid", "europe", "emea", "worldwide", "global", "multiple locations")


def place(location: str) -> str:
    """The first place named, lowercase. "" when the listing names only a country."""
    first = " ".join((location or "").casefold().split()).split(";")[0].split(",")[0].strip()
    return "" if not first or COUNTRY_ONLY.fullmatch(first) else first


def same_place(a: str, b: str) -> bool:
    """Whether two listings are about one place. A country-only location ("United
    Kingdom") is compatible with any city in it; two named cities are not."""
    x, y = place(a), place(b)
    return not x or not y or x == y or x in y or y in x


def keys_for(company: str, title: str, url: str, location: str = "") -> list[str]:
    """Both ways one posting can be recognised: its platform posting id, and the
    company plus this exact title and place. The second catches one programme an
    aggregator lists twice under different ids; it stays exact on purpose, because
    two roles at one employer that differ only by team ("Forward Deployed
    Engineer, Air Defense" and ", Space") are separate applications."""
    spelling = " ".join((title or "").casefold().split())
    keys = [f"{normalize.company(company)[1]}|{spelling}"]
    posting = normalize.posting_key(url)
    if posting:
        keys.insert(0, posting)
    return keys


def row_from_job(j: dict) -> dict:
    title, desc = j.get("job_title") or "", j.get("description") or ""
    refs = j.get("sources") or []
    official = j.get("application_url") or ""
    external = next((r.get("source_url") for r in refs
                     if r.get("source") in ("trackr", "gradcracker", "brightnetwork", "higherin", "targetjobs")
                     and r.get("source_url")), "")
    deadline = (j.get("deadline") or "")[:10]
    return {"COMPANY": normalize.company(j.get("company") or "")[0], "ROLE": title,
            "LOCATION": j.get("location") or "", "WORK TYPE": j.get("work_type") or "",
            "INTERNSHIP DURATION": j.get("duration") or "Not stated",
            "START DATE": (j.get("start_date") or "")[:10] or "Not stated",
            "DATE POSTED": (j.get("date_posted") or "")[:10] or "Not stated",
            "APPLICATION DEADLINE": deadline or "Not stated",
            "ELIGIBILITY / YEAR REQUIREMENT": trim(
                " | ".join(x for x in [j.get("year_of_study"), j.get("degree_requirements"),
                                       f"expected graduation: {j.get('expected_graduation')}"
                                       if j.get("expected_graduation") else "",
                                       f"visa sponsorship: {j.get('visa_sponsorship')}"
                                       if j.get("visa_sponsorship") else ""] if x), 400) or "Not stated",
            "DESCRIPTION / RESPONSIBILITIES": trim(desc),
            "SOURCE": ", ".join(sorted({r.get("source", "") for r in refs})) or j.get("primary_source") or "",
            "OFFICIAL APPLICATION URL": official, "EXTERNAL LISTING URL": external or "",
            "STATUS": j.get("open_status") or "UNKNOWN",
            "DATE FOUND": (j.get("date_found") or "")[:10],
            "_eligible": j.get("eligible_for_you") or "", "_reason": j.get("eligibility_reason") or "",
            "_year_text": " ".join(x for x in (j.get("year_of_study"), j.get("university_year")) if x),
            "_text": f"{title}. {desc}", "_title": title, "_deadline": deadline,
            "_pipeline": True,
            "_keys": keys_for(j.get("company") or "", title, official, j.get("location") or "")}


def row_from_sweep(r: dict) -> dict:
    title, desc = r.get("title") or "", r.get("description") or ""
    return {"COMPANY": normalize.company(r.get("company") or "")[0], "ROLE": title,
            "LOCATION": r.get("location") or "",
            "WORK TYPE": level(title), "INTERNSHIP DURATION": "Not stated", "START DATE": "Not stated",
            "DATE POSTED": (r.get("posted") or "")[:10] or "Not stated",
            "APPLICATION DEADLINE": (r.get("deadline") or "")[:10] or "Not stated",
            "ELIGIBILITY / YEAR REQUIREMENT": "Not stated - read the posting",
            "DESCRIPTION / RESPONSIBILITIES": trim(desc),
            "SOURCE": f"company site ({r.get('platform')})", "OFFICIAL APPLICATION URL": r.get("url") or "",
            "EXTERNAL LISTING URL": "", "STATUS": "UNKNOWN", "DATE FOUND": str(TODAY),
            "_eligible": "", "_reason": "", "_text": f"{title}. {desc}", "_title": title,
            "_deadline": (r.get("deadline") or "")[:10],
            "_pipeline": False,
            "_keys": keys_for(r.get("company") or "", title, r.get("url") or "", r.get("location") or "")}


jobs = json.loads((config.STATE_DIR / "last_run.json").read_text(encoding="utf-8"))["jobs"]

# Programmes the workbook's own technical filter removed as non-technical (a
# finance or treasury placement whose description happens to say "optimisation")
# do not belong in a target-area sheet either.
import csv as _csv
NON_TECHNICAL = ROOT / "data" / "checkpoint" / "removed-non-technical.csv"
not_technical = {x["ID"] for x in _csv.DictReader(NON_TECHNICAL.open(encoding="utf-8-sig"))}     if NON_TECHNICAL.exists() else set()
jobs = [j for j in jobs if j.get("id") not in not_technical]
print(f"non-technical programmes skipped: {len(not_technical)}")
sweep = json.loads(SWEEP.read_text(encoding="utf-8")) if SWEEP.exists() else {"rows": [], "failed": [], "unresolved": []}

# The first FDE sweep kept only a 600-character extract of each posting. It is
# read last, so a row the fuller sweep also found keeps the fuller description.
earlier = []
if FDE_CSV.exists():
    import csv
    for x in csv.DictReader(FDE_CSV.open(encoding="utf-8-sig")):
        earlier.append({"company": x["Company"], "platform": x["Platform"], "title": x["Job Title"],
                        "location": x["Location"], "team": x["Team"], "url": x["Application URL"],
                        "description": x["What the posting says"], "deadline": ""})
print(f"pipeline rows: {len(jobs)} | board sweep rows: {len(sweep['rows'])} | earlier FDE rows: {len(earlier)}")

rows, seen, duplicates = [], {}, 0
for source_rows in ([row_from_job(j) for j in jobs], [row_from_sweep(r) for r in sweep["rows"]],
                    [row_from_sweep(r) for r in earlier]):
    for row in source_rows:
        cats = categories(row["_title"], row["_text"])   # the full posting, not the trimmed extract
        if not cats:
            continue
        first = next((seen[k] for k in row["_keys"] if k in seen), None)
        if first is not None and not same_place(first["LOCATION"], row["LOCATION"]):
            # One employer runs the same programme in several cities, and each is
            # its own application. Two named cities are two rows.
            row["_keys"] = [f"{k}|{row['LOCATION'].casefold()}" for k in row["_keys"]]
            # The same posting read twice in this city is still one row.
            first = next((seen[k] for k in row["_keys"] if k in seen), None)
        if first is not None:                 # same posting on another platform
            duplicates += 1
            if not first["EXTERNAL LISTING URL"] and row["OFFICIAL APPLICATION URL"] != first["OFFICIAL APPLICATION URL"]:
                first["EXTERNAL LISTING URL"] = row["OFFICIAL APPLICATION URL"]
            stated = [d for d in (first["DATE POSTED"], row["DATE POSTED"]) if d != "Not stated"]
            if stated:
                first["DATE POSTED"] = min(stated)
            if row["SOURCE"] not in first["SOURCE"]:
                first["SOURCE"] = f"{first['SOURCE']}, {row['SOURCE']}"
            # One posting read twice: what either copy says is what the posting
            # says. An older copy can lack a section a newer read picked up
            # (Lever's "What We Require" list, before the reader kept it).
            if row["_text"] not in first["_text"]:
                first["_text"] = f"{first['_text']} {row['_text']}"
            continue
        text = row["_text"]
        row["ROLE CATEGORY"] = ", ".join(cats)
        row["PROGRAMMING LANGUAGES"] = ", ".join(languages_in(text)) or "Not stated"
        row["KEY TECHNICAL SKILLS"] = ", ".join(sorted(found(text, TOOLS_RE))) or "Not stated"
        row["KEY KNOWLEDGE AREAS"] = ", ".join(sorted(found(text, KNOW_RE))) or "Not stated"
        row["_level"] = level(row["_title"], row["WORK TYPE"])
        row["_uk"] = uk_ish(row["LOCATION"])
        row["_uk_ok"] = row["_uk"] == "UK" or (row["_pipeline"] and row["_uk"] != "Not UK")
        row["_cats"] = cats
        notes = []
        if row["_reason"]:
            notes.append(row["_reason"])
        if row["DESCRIPTION / RESPONSIBILITIES"] in ("", "Not stated"):
            notes.append("no description on the feed - open the posting")
        if row["_uk"] == "Not stated":
            notes.append("location not stated - check it is UK")
        row["NOTES"] = trim(" | ".join(notes), 300)

        # Match, per criterion, only where the posting says something.
        primary = {"Algorithm Engineer", "Forward Deployed Engineer", "AI / ML"} & set(cats)
        strong_title = any(p.search(row["_title"]) for p in (ALGO_STRONG, FDE_TITLE, AIML_STRONG))
        row["MATCH: ROLE"] = "High" if primary and strong_title else "Medium" if primary else "Low"
        langs = row["PROGRAMMING LANGUAGES"]
        row["MATCH: LANGUAGES"] = ("High" if ("Python" in langs or "C++" in langs)
                                   else "Not stated" if langs == "Not stated" else "Medium")
        know = row["KEY KNOWLEDGE AREAS"]
        row["MATCH: KNOWLEDGE"] = ("High" if any(k in know for k in
                                                 ("machine learning", "deep learning", "algorithms",
                                                  "computer vision", "data structures"))
                                   else "Not stated" if know == "Not stated" else "Medium")
        row["MATCH: ELIGIBILITY"] = {"Yes": "High", "No": "Low", "REQUIRES VERIFICATION": "Verify"}.get(
            row["_eligible"], "Verify")
        row["MATCH: LOCATION"] = {"UK": "High", "Not UK": "Low", "Not stated": "Not stated"}[row["_uk"]]
        due = row["_deadline"]
        row["MATCH: TIMING"] = ("High" if due and due >= str(TODAY) else "Low" if due else "Not stated")
        for key in row["_keys"]:
            seen.setdefault(key, row)
        rows.append(row)

print(f"classified into a target area: {len(rows)} | duplicate postings merged: {duplicates}")


def closed(row) -> bool:
    return row["STATUS"] == "CLOSED" or (row["_deadline"] and row["_deadline"] < str(TODAY))


# ---------------------------------------------------------------- FDE: current undergraduates only
#: A student-level role, from its title: the kind a current undergraduate applies to.
STUDENT_ROLE = words("intern", "internship", "placement", "industrial placement", "year in industry",
                     "student", "insight", "spring week", "summer analyst", "co-op", "undergraduate")
#: Roles that start after graduating, or need experience first. Their title decides.
NOT_FOR_STUDENTS = words("new grad", "new graduate", "graduate", "graduate programme", "graduate program",
                         "senior", "staff", "principal", "lead", "manager", "director", "head", "vp",
                         "vice president", "architect", "experienced")
#: Explicit statements that the applicant is a student now. Each is quoted as the evidence.
STILL_STUDYING = [
    re.compile(r"(?i)(?:must be |be |are |you're |you are )?(?:planning on |expected to be |expecting to be )?"
               r"graduating (?:in |between |by )?(?:(?:december|spring|summer|autumn|fall|winter|[a-z]+) )?"
               r"20\d\d(?:\s*(?:or|and|/|-|to)\s*(?:(?:december|spring|summer|autumn|fall|winter) )?20\d\d)?"),
    # "Pursuing or received a degree" accepts someone still studying, in so many words.
    re.compile(r"(?i)(?:currently )?(?:pursuing|enrolled in|studying for|working towards)"
               r"(?: or (?:received|completed|obtained))? (?:a |an |your )?"
               r"(?:bachelor'?s?|undergraduate|university|b\.?sc|b\.?s\.?|b\.?eng|degree)[^.;]{0,60}"),
    re.compile(r"(?i)current(?:ly)? (?:an? )?(?:undergraduate|university student)s?"),
    re.compile(r"(?i)undergraduate students?[^.;]{0,60}"),
    re.compile(r"(?i)penultimate[- ]year[^.;]{0,60}"),
    re.compile(r"(?i)(?:first|second|third|final)[- ]year (?:undergraduate|student)s?[^.;]{0,40}"),
    re.compile(r"(?i)(?:final|last) internship before graduating"),
    re.compile(r"(?i)returning to (?:university|school|your studies|full[- ]time study)[^.;]{0,40}"),
    re.compile(r"(?i)enrolled (?:in|at) (?:a |an )?(?:university|college|bachelor'?s?|undergraduate)[^.;]{0,60}"),
    re.compile(r"(?i)class of 20\d\d"),
]
#: Graduation that must come before the role starts: new-grad wording.
ALREADY_GRADUATED = re.compile(r"(?i)graduating in [^.;]{0,30}\bor before\b|completed (?:a |your )?(?:bachelor|undergraduate|degree)")
#: Multiple years of experience: an experienced hire, whatever else the posting says.
YEARS_REQUIRED = re.compile(r"(?i)(\d+)\s*\+?\s*(?:or more )?years? of (?:[a-z-]+ ){0,4}experience")


def undergrad_evidence(row: dict) -> tuple[bool, str, str]:
    """(include?, the posting's own words proving it, why not).

    An FDE role qualifies only if its posting says a current student may apply:
    a student-level role with an explicit statement ("Must be planning on
    graduating in 2028"), or any role that says in so many words that current
    undergraduates are eligible. The title alone never qualifies a role; new-grad,
    graduate-only and experienced-hire roles never do. Unclear is excluded.
    """
    title, text = row["_title"], row["_text"]
    if NOT_FOR_STUDENTS.search(title) and not re.search(r"(?i)undergraduate", title):
        return False, "", "title is a graduate / experienced role"
    many = [int(m.group(1)) for m in YEARS_REQUIRED.finditer(text) if int(m.group(1)) >= 2]
    if many:
        return False, "", f"requires {max(many)}+ years of experience"
    if ALREADY_GRADUATED.search(text):
        return False, "", "requires graduating before the role starts"
    found = [m.group(0).strip() for pattern in STILL_STUDYING for m in pattern.finditer(text)]
    if not found:
        return False, "", "posting does not say current students may apply"
    student_role = bool(STUDENT_ROLE.search(title))
    # A graduation year alone does not make a full-time role a student one.
    explicit = [f for f in found if "graduating" not in f.lower() and "class of" not in f.lower()]
    if not student_role and not explicit:
        # A full-time role naming a graduation year is a new-grad hire.
        return False, "", "not a student role, and no explicit statement that undergraduates are eligible"
    evidence = "; ".join(dict.fromkeys(f[:140] for f in found))[:400]
    return True, evidence, ""


#: Programme types that are undergraduate by definition in the UK: nobody but a
#: current student takes a placement year or a spring week.
UNDERGRAD_PROGRAMME = words("industrial placement", "placement year", "year in industry", "year-in-industry",
                            "sandwich placement", "sandwich year", "undergraduate placement",
                            "undergraduate internship", "undergraduate programme", "undergraduate program",
                            "student placement", "spring week", "spring insight", "insight programme",
                            "insight week", "summer placement")
#: A posting for doctoral or master's students only is not for an undergraduate.
POSTGRAD_ONLY = re.compile(r"(?i)\b(?:ph\.?d|doctoral|doctorate|master'?s|msc|m\.sc)\b")
BACHELOR_TOO = re.compile(r"(?i)\b(?:bachelor|undergraduate|penultimate|b\.?sc|b\.?eng|first[- ]year|"
                          r"second[- ]year|final[- ]year|year in industry|placement year)\b")
#: Trackr and Gradcracker list student programmes only; their programme type is
#: the platform saying so.
STUDENT_LISTING = ("trackr", "gradcracker", "brightnetwork", "higherin", "targetjobs")


def undergrad_general(row: dict) -> tuple[bool, str, str]:
    """(include?, evidence, why not) for Algorithm and AI roles.

    Evidence, strongest first: the posting's own statement that a current
    student may apply; the year of study the pipeline read from the posting; a
    programme type that is undergraduate by definition; a student-programme
    platform listing it as a summer internship, placement or spring week.
    Graduate-only, PhD/master's-only and 2+-years-experience roles are out,
    and a role with none of the evidence above is out rather than guessed in.
    """
    title, text = row["_title"], row["_text"]
    if NOT_FOR_STUDENTS.search(title) and not re.search(r"(?i)undergraduate", title):
        return False, "", "title is a graduate / experienced role"
    many = [int(m.group(1)) for m in YEARS_REQUIRED.finditer(text) if int(m.group(1)) >= 2]
    if many:
        return False, "", f"requires {max(many)}+ years of experience"
    if ALREADY_GRADUATED.search(text):
        return False, "", "requires graduating before the role starts"
    if POSTGRAD_ONLY.search(f"{title} {text}") and not BACHELOR_TOO.search(f"{title} {text}"):
        return False, "", "for PhD / master's students only"
    stated = [m.group(0).strip() for pattern in STILL_STUDYING for m in pattern.finditer(text)]
    if stated:
        return True, "; ".join(dict.fromkeys(s[:140] for s in stated))[:400], ""
    if row.get("_year_text"):
        return True, f"posting: {trim(row['_year_text'], 200)}", ""
    programme = UNDERGRAD_PROGRAMME.search(title) or UNDERGRAD_PROGRAMME.search(row.get("WORK TYPE") or "")
    if programme:
        return True, f"programme type: {programme.group(0)} (undergraduate by definition)", ""
    platforms = [s for s in STUDENT_LISTING if s in (row.get("SOURCE") or "").casefold()]
    kind = row.get("WORK TYPE") or ""
    if platforms and kind in ("Summer Internship", "Industrial Placement", "Spring Week"):
        return True, f"listed as a student {kind} on {', '.join(platforms)}", ""
    return False, "", "undergraduate eligibility not stated"


def eligible_rows(wanted_cats: set) -> tuple[list, list]:
    """Student-level UK rows in these categories, split by undergraduate evidence."""
    kept, dropped = [], []
    for r in rows:
        if not (wanted_cats & set(r["_cats"])) or r["_level"] != "Internship / placement" or not r["_uk_ok"]:
            continue
        ok, evidence, why = undergrad_general(r)
        if not ok:
            dropped.append((r, why))
            continue
        r[FDE_EVIDENCE] = evidence
        kept.append(r)
    return kept, dropped


PROFILE_YEAR = str(config.load_profile().get("graduation_year") or "")


def fde_rows():
    """Every FDE posting, split into those a current undergraduate may apply to
    and those excluded - with the reason, so the exclusions can be checked."""
    kept, dropped = [], []
    for r in rows:
        # The role itself must be FDE: a Product Designer or plain Software
        # Engineer internship whose description mentions deployment is not one.
        if "Forward Deployed Engineer" not in r["_cats"] or not FDE_TITLE.search(r["_title"]):
            continue
        ok, evidence, why = undergrad_evidence(r)
        if not ok:
            dropped.append((r, why))
            continue
        r["UNDERGRADUATE ELIGIBILITY EVIDENCE"] = evidence
        years = set(re.findall(r"20\d\d", evidence))
        if years and PROFILE_YEAR:
            r["MATCH: ELIGIBILITY"] = "High" if PROFILE_YEAR in years else "Low"
            if PROFILE_YEAR not in years:
                r["NOTES"] = trim(f"posting asks for graduation in {'/'.join(sorted(years))}; "
                                  f"your profile says {PROFILE_YEAR}" + (f" | {r['NOTES']}" if r["NOTES"] else ""), 300)
        kept.append(r)
    return kept, dropped


def for_sheet(name):
    if name == "Forward-Deployed Engineer":
        picked = fde_rows()[0]
    elif name == "Algorithm Engineer":
        picked = eligible_rows({"Algorithm Engineer"})[0]
    else:
        picked = eligible_rows({"AI / ML", "Computer Vision", "AI Software Engineering"})[0]
    # A role whose deadline has passed is left off; no deadline stated, it stays.
    picked = [r for r in picked if not (r["_deadline"] and r["_deadline"] < str(TODAY))]
    active = sorted((r for r in picked if not closed(r)),
                    key=lambda r: (r["_deadline"] or "9999", r["COMPANY"].casefold(), r["ROLE"].casefold()))
    gone = sorted((r for r in picked if closed(r)), key=lambda r: (r["COMPANY"].casefold(), r["ROLE"].casefold()))
    return active, gone


# ---------------------------------------------------------------- write
#: Written as real dates, blank when unknown: Excel's date filter and sort need
#: every cell in the column to be a date. "Not stated" text there breaks both.
DATE_COLUMNS = ("START DATE", "DATE POSTED", "APPLICATION DEADLINE", "DATE FOUND")


def cell_value(row: dict, title: str):
    value = row.get(title, "")
    if title in DATE_COLUMNS:
        text = str(value or "")[:10]
        return date.fromisoformat(text) if re.fullmatch(r"\d{4}-\d\d-\d\d", text) else None
    return value


if (ROOT / "data" / "~$internships.xlsx").exists():
    sys.exit("Excel has the workbook open - close it first.")
wb = load_workbook(WORKBOOK)
before = [s for s in wb.sheetnames if s not in SHEETS]
written = {}
for position, name in enumerate(SHEETS, start=1):
    if name in wb.sheetnames:
        del wb[name]
    ws = wb.create_sheet(name, position)
    columns = sheet_columns(name)
    for col, title in enumerate(columns, start=1):
        cell = ws.cell(1, col, title)
        cell.fill, cell.font = HEADER_FILL, HEADER_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(col)].width = WIDTHS[title]
    active, gone = for_sheet(name)
    r = 1
    # Closed rows follow the active ones with no banner row between: a banner
    # (merged or not) is a row Excel would sort and filter as if it were data.
    # STATUS says CLOSED instead.
    for block in (active, gone):
        for row in block:
            r += 1
            for col, title in enumerate(columns, start=1):
                cell = ws.cell(r, col, cell_value(row, title))
                if title in DATE_COLUMNS:
                    cell.number_format = "yyyy-mm-dd"
                cell.alignment = Alignment(vertical="top", wrap_text=title in (
                    "ROLE", "ROLE CATEGORY", "ELIGIBILITY / YEAR REQUIREMENT", "PROGRAMMING LANGUAGES",
                    "KEY TECHNICAL SKILLS", "KEY KNOWLEDGE AREAS", "DESCRIPTION / RESPONSIBILITIES",
                    "NOTES", "LOCATION", FDE_EVIDENCE))
                if title in ("OFFICIAL APPLICATION URL", "EXTERNAL LISTING URL") and str(cell.value).startswith("http"):
                    cell.hyperlink = cell.value
                    cell.font = Font(color="0563C1", underline="single")
                if title.startswith("MATCH: ") and MATCH_FILL.get(cell.value):
                    cell.fill = PatternFill("solid", fgColor=MATCH_FILL[cell.value])
                if block is gone:
                    cell.fill = BANNER_FILL
            ws.row_dimensions[r].height = 42
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{max(r, 2)}"
    written[name] = (len(active), len(gone))
    print(f"{name}: {len(active)} active, {len(gone)} closed/monitor")

wb.save(DRAFT)

v = load_workbook(DRAFT)
problems = []
if [s for s in v.sheetnames if s not in SHEETS] != before:
    problems.append(("other sheets changed", v.sheetnames))
original = load_workbook(WORKBOOK)
for sheet in before:
    a, b = original[sheet], v[sheet]
    if (a.max_row, a.max_column) != (b.max_row, b.max_column):
        problems.append((sheet, "size changed"))
    if {str(m) for m in a.merged_cells.ranges} != {str(m) for m in b.merged_cells.ranges}:
        problems.append((sheet, "merged cells changed"))
for name in SHEETS:
    check = v[name]
    if [c.value for c in check[1]] != sheet_columns(name):
        problems.append((name, "header"))
    active, gone = for_sheet(name)
    if check.max_row - 1 < len(active):
        problems.append((name, "rows missing", check.max_row - 1, len(active)))
    if check.merged_cells.ranges:
        problems.append((name, "merged cells", [str(m) for m in check.merged_cells.ranges][:3]))
    for r in range(2, check.max_row + 1):
        company = check.cell(r, 1).value or ""
        if company and not check.cell(r, 3).value:
            problems.append((name, r, "row with no ROLE CATEGORY"))
        for title in DATE_COLUMNS:
            value = check.cell(r, sheet_columns(name).index(title) + 1).value
            if value is not None and not isinstance(value, (date, datetime)):
                problems.append((name, r, title, "not a date", value))
print("verification problems:", len(problems))
for p in problems[:20]:
    print("   ", p)
if problems:
    sys.exit("VERIFICATION FAILED - workbook NOT changed. Draft: " + str(DRAFT))

shutil.copy2(WORKBOOK, BACKUP)
shutil.copy2(DRAFT, WORKBOOK)

import csv as _csv_out  # noqa: E402
kept_fde, dropped_fde = fde_rows()
with (ROOT / "data" / "checkpoint" / "fde-excluded.csv").open("w", newline="", encoding="utf-8-sig") as fh:
    w = _csv_out.writer(fh)
    w.writerow(["Company", "Role", "Location", "Why excluded", "Official URL"])
    for r, why in sorted(dropped_fde, key=lambda x: (x[1], x[0]["COMPANY"].casefold())):
        w.writerow([r["COMPANY"], r["ROLE"], r["LOCATION"], why, r["OFFICIAL APPLICATION URL"]])
from collections import Counter as _Counter  # noqa: E402
print(f"FDE: {len(kept_fde)} kept for current undergraduates | {len(dropped_fde)} excluded: "
      f"{dict(_Counter(why for _, why in dropped_fde))}")
for label, cats in (("Algorithm Engineer", {"Algorithm Engineer"}),
                    ("AI roles", {"AI / ML", "Computer Vision", "AI Software Engineering"})):
    k, d = eligible_rows(cats)
    print(f"{label}: {len(k)} kept | {len(d)} excluded: {dict(_Counter(why for _, why in d))}")
    with (ROOT / "data" / "checkpoint" / f"{label.split()[0].lower()}-excluded.csv").open(
            "w", newline="", encoding="utf-8-sig") as fh:
        w = _csv_out.writer(fh)
        w.writerow(["Company", "Role", "Location", "Why excluded", "Official URL"])
        for r, why in sorted(d, key=lambda x: (x[1], x[0]["COMPANY"].casefold())):
            w.writerow([r["COMPANY"], r["ROLE"], r["LOCATION"], why, r["OFFICIAL APPLICATION URL"]])
report = {"generated": datetime.now().isoformat(timespec="seconds"), "sheets": written,
          "classified": len(rows), "duplicates_merged": duplicates,
          "boards_failed": sweep["failed"], "candidates_unresolved": sweep["unresolved"]}
(ROOT / "data" / "checkpoint" / "target-sheets-report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
print("sheets written. backup:", BACKUP)
print("sheets now:", load_workbook(WORKBOOK, read_only=True).sheetnames)
