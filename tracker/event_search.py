"""This week's search plan for the event researcher.

Claude runs the searches (skills/event-researcher/SKILL.md). This decides
which ones, so the discovery matrix (format x domain x organisation x place x
source) is covered systematically: short lists every week, long lists in a
rotation that reaches every entry within rotation_weeks. The vocabulary is
config/event_search.yaml and is examples, never a whitelist.
"""

from __future__ import annotations

from datetime import date

from tracker import config
from tracker import events as ev

_SOCIETIES = ('(CompSoc OR "Computing Society" OR "AI Society" OR "Robotics Society"'
              ' OR "Cyber Society")')
_UNCAPPED = {0, 9, 10}  # re-checks, leads and recurring follow-ups are never cut


def _slice(items: list, today: date, weeks: int, offset: int) -> list:
    k = (today.isocalendar().week + offset) % weeks
    return items[k::weeks]


def sweep_plan(store: list[dict], leads: list[dict], today: date, offset: int = 0) -> list[dict]:
    """Rounds in the order the skill runs them. offset=1, 2... gives the next
    rotation slices, used for extra rounds until the sweep saturates."""
    s = config.load("event_search")
    rot = lambda items: _slice(items, today, s["rotation_weeks"], offset)  # noqa: E731
    years = f"{today.year} OR {today.year + 1}"
    unis = rot(s["universities"])
    companies = rot([c for group in s["companies"].values() for c in group])

    rounds = [
        (0, "Re-check due rows and resolve leads", "",
         [f'{e["id"]} {e["name"]} {e["official_url"] or e["source_url"]}'
          for e in ev.due_for_check(store, today)]
         + [" ".join(f'"{l["name"]}" {l.get("organisation", "")} official page'.split())
            for l in leads]),
        (1, "Format x UK", "Event Format Search",
         [f'"{f}" UK students {years}' for f in s["formats"]]
         + [f"athon {q} UK students" for q in s["athon_qualifiers"]]),
        (2, "Format x city", "City Search",
         [f'"{f}" {c}' for c in rot(s["cities"]) for f in s["city_formats"]]),
        (3, "Technical domain x UK", "Technical Domain Search",
         [f'"{d}" student competition OR challenge UK' for d in s["domains"]]),
        (4, "University x format", "University Search",
         [q for u in unis for q in (f'"{u}" hackathon OR buildathon OR datathon',
                                    f'"{u}" coding OR programming competition',
                                    f'"{u}" AI OR data OR robotics OR engineering challenge')]),
        (5, "Society x format", "Society Search",
         [f'"{u}" {_SOCIETIES} competition OR challenge OR hackathon' for u in unis]
         + [f'"{t}" competition OR challenge OR sprint UK' for t in rot(s["society_types"])]),
        (6, "Company x technical event", "Company Search",
         [q for c in companies for q in (
             f"{c} student coding OR engineering OR AI OR data challenge UK",
             f"{c} innovation OR developer OR build challenge OR hackathon UK")]),
        (7, "Organisation x technical domain", "Organisation Search",
         [f'"{o}" AI OR data OR robotics OR cyber OR software competition OR challenge students'
          for o in rot(s["organisations"])]),
        (8, "Platforms", "Platform Search",
         [f"site:{p} UK {years}" for p in s["platforms"]]),
        (9, "Next editions of past events", "Recurring Event Search",
         [q for e in ev.recurring_followups(store, today) for q in (
             f'"{ev.series_name(e["name"])}" {ev.edition_date(e).year + 1}',
             f'"{ev.series_name(e["name"])}" next event registration')]),
        (10, "Follow up this sweep's finds (organiser's other events, series)",
         "Previous Event Follow-up", []),
    ]
    cap = s["per_round_cap"]
    return [{"round": n, "name": name, "discovery_method": method,
             "queries": qs if n in _UNCAPPED else qs[:cap]}
            for n, name, method, qs in rounds]
