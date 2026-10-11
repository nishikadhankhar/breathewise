"""What changed between two plans for the same day.

The evening plan is made from a forecast; by morning the forecast has moved.
Staff should only hear about the decisions that are different.
"""

from datetime import datetime, timezone


def snapshot(plan):
    """The decisions in a plan, small enough to store and compare later."""
    return {
        "date": plan["date"],
        "made_at": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "peak_aqi": plan["peak_aqi"],
        "actions": [
            {"class": a["class"], "activity": a["activity"], "start": a["start"], "end": a["end"],
             "action": a["action"], "to": (a.get("swap_with") or {}).get("start"), "aqi": a["aqi"]}
            for a in plan["actions"] if a["action"] in ("ok", "swap", "indoors")
        ],
    }


def describe(entry):
    if entry["action"] == "swap":
        return f"moves to {entry['to']}"
    return "held indoors" if entry["action"] == "indoors" else "goes ahead outdoors"


def compare(old, new):
    """Decisions that differ between two snapshots of the same day, in timetable order."""
    key = lambda a: (a["class"], a["activity"], a["start"])
    before = {key(a): a for a in old["actions"]}
    changes = []
    for now in new["actions"]:
        was = before.get(key(now))
        if was and (was["action"], was["to"]) != (now["action"], now["to"]):
            changes.append({"class": now["class"], "activity": now["activity"], "start": now["start"], "end": now["end"],
                            "was": describe(was), "now": describe(now), "aqi_was": was["aqi"], "aqi_now": now["aqi"]})
    return sorted(changes, key=lambda c: (c["start"], c["class"]))
