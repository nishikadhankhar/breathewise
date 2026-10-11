"""Compare three ways of running a timetable through a whole winter.

For every school day of the last complete November-January, the same outdoor
periods are scored under:

  as_scheduled  change nothing
  noon_rule     the obvious blanket rule: no outdoor activity before 12:00
  breathewise   this app's plan for that day

All three are scored on the air model's record of each day. BreatheWise also
planned from that record, so its result here is a best case: with a real
forecast some of its moves would land in air that turned out worse.
"""

from datetime import date

from .planner import build_plan

NOON = 12 * 60
LABELS = {
    "as_scheduled": "Timetable as it is",
    "noon_rule": "No outdoor activity before 12:00",
    "breathewise": "BreatheWise plan",
}


def last_winter(today):
    """First and last day of the most recent November-January that is over."""
    year = today.year if today.month >= 2 else today.year - 1
    return date(year - 1, 11, 1), date(year, 1, 31)


def backtest(periods, by_date, thresholds=None, asthma=None):
    """`by_date` is {"2025-11-01": {hour: aqi}}. Minutes are class-minutes of outdoor activity."""
    totals = {key: {"outdoor_minutes": 0, "over_limit_minutes": 0} for key in LABELS}
    needless = 0        # within-limit outdoor minutes the noon rule cancels anyway
    days = days_rule_wrong = days_with_changes = 0
    for stamp in sorted(by_date):
        plan = build_plan(periods, by_date[stamp], date.fromisoformat(stamp), thresholds, asthma=asthma)
        actions = [a for a in plan["actions"] if a["action"] != "unknown"]
        if not actions:
            continue
        days += 1
        days_with_changes += bool(plan["changes"])
        rule_wrong = False
        for a in actions:
            minutes, over = a["minutes"], a["aqi"] > a["limit"]
            totals["as_scheduled"]["outdoor_minutes"] += minutes
            totals["as_scheduled"]["over_limit_minutes"] += minutes if over else 0
            if a["start"] >= "12:00":
                totals["noon_rule"]["outdoor_minutes"] += minutes
                totals["noon_rule"]["over_limit_minutes"] += minutes if over else 0
                rule_wrong = rule_wrong or over
            elif not over:
                needless += minutes
                rule_wrong = True
            if a["action"] in ("ok", "swap"):   # a swap lands in a slot under the limit
                totals["breathewise"]["outdoor_minutes"] += minutes
        days_rule_wrong += rule_wrong
    scheduled = totals["as_scheduled"]["outdoor_minutes"]
    return {
        "school_days": days,
        "days_with_changes": days_with_changes,
        "days_noon_rule_wrong": days_rule_wrong,
        "scheduled_outdoor_minutes": scheduled,
        "noon_rule_needless_minutes": needless,
        "strategies": [{"key": key, "label": LABELS[key], **totals[key],
                        "outdoor_kept_percent": round(100 * totals[key]["outdoor_minutes"] / scheduled) if scheduled else None}
                       for key in LABELS],
    }
