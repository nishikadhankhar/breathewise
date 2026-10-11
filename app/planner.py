"""Turn a timetable and hourly AQI into period-level decisions.

This is deliberately rule-based: every number and every swap in the plan comes
from here, never from a language model.
"""

import re
from datetime import timedelta

from .aqi import category
from .timetable import DAYS, WHOLE_SCHOOL, fmt_time

DEFAULT_THRESHOLDS = {
    # Outdoor activity is not allowed above these AQI values. Schools set their own.
    # 200 is the top of CPCB's "Moderate" band; "Poor" (201-300) is where CPCB says most
    # people get breathing discomfort on prolonged exposure.
    "primary_max_grade": 5,
    "primary_limit": 200,
    "senior_limit": 200,
    # A forecast can be off, so a period is only moved into a slot that is this far under
    # the limit, and periods this close to the limit are flagged as borderline.
    "forecast_margin": 30,
    # Above this (the start of CPCB's "Moderate" band) children with asthma or other
    # breathing conditions should keep to light activity.
    "sensitive_limit": 100,
    # How many classes can be outdoors at once (ground, PT teacher). 0 = as many as the
    # school's own timetable ever puts outside together.
    "max_outdoor_classes": 0,
    # A class with children who have asthma uses this limit when it is the lower one.
    # It sits midway through "Moderate"; there is no official figure, so schools should set it.
    "asthma_limit": 150,
    # 1 = never move an outdoor period into a class's last period of the day.
    "avoid_last_period": 0,
    # 1 = never give one teacher two outdoor periods back to back because of a move.
    "avoid_back_to_back": 0,
    # A class's outdoor activity for the whole day, as "minutes at its limit": minutes x AQI may
    # not add up to more than this many minutes at the limit. Four periods at 190 pass a limit of
    # 200 one by one but not this. No official figure exists; schools should set it. 0 = off.
    "daily_outdoor_minutes": 90,
}

ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8, "IX": 9, "X": 10, "XI": 11, "XII": 12}
PRE_PRIMARY = re.compile(r"\b(NURSERY|LKG|UKG|KG|PREP|PRE|PLAY|KINDERGARTEN)\b")


def grade_of(klass):
    """School grade from a class name such as 7B, VII-B, Class 3 or UKG. None if unknown."""
    name = re.sub(r"\b(CLASS|GRADE|STD|STANDARD)\b\.?", " ", klass.upper()).strip()
    if PRE_PRIMARY.search(name):
        return 0
    digits = re.search(r"\d+", name)
    if digits:
        return int(digits.group())
    roman = re.match(r"(XII|XI|IX|X|VIII|VII|VI|IV|V|III|II|I)\b", name)
    return ROMAN[roman.group(1)] if roman else None


def limit_for(klass, thresholds, asthma=None):
    """AQI limit for a class. Whole-school events and unknown classes use the strictest limit.

    `asthma` maps class names to the number of children with asthma; those classes, and any
    whole-school event they take part in, are held to the asthma limit if it is lower.
    """
    asthma = asthma or {}
    grade = None if klass == WHOLE_SCHOOL else grade_of(klass)
    if grade is None:
        limit = min(thresholds["primary_limit"], thresholds["senior_limit"])
    elif grade <= thresholds["primary_max_grade"]:
        limit = thresholds["primary_limit"]
    else:
        limit = thresholds["senior_limit"]
    affected = any(asthma.values()) if klass == WHOLE_SCHOOL else asthma.get(klass, 0)
    return min(limit, thresholds["asthma_limit"]) if affected else limit


def period_aqi(period, hourly):
    """Worst hourly AQI the period overlaps, or None if there is no data."""
    hours = range(period.start // 60, (period.end - 1) // 60 + 1)
    values = [hourly[h] for h in hours if h in hourly]
    return max(values) if values else None


def _describe(period, aqi):
    return {
        "activity": period.activity,
        "minutes": period.duration,
        "start": fmt_time(period.start),
        "end": fmt_time(period.end),
        "aqi": aqi,
    }


def usual_outdoor_classes(periods):
    """The most classes the school's own timetable ever has outdoors at the same time."""
    most = 1
    outdoor = [p for p in periods if p.outdoor and p.klass != WHOLE_SCHOOL]
    for p in outdoor:
        together = {q.klass for q in outdoor if q.day == p.day and q.start < p.end and p.start < q.end}
        most = max(most, len(together))
    return most


def build_plan(periods, hourly, day, thresholds=None, now=None, current_aqi=None, sizes=None, asthma=None):
    """Plan one school day.

    `day` is a datetime.date. Pass `now` (minutes from midnight, school local
    time) when the day is today: finished periods are then left alone, a period
    in progress is judged on the current AQI, and swaps only use slots that
    have not started. `sizes` ({"school": n, "per_class": n}) lets the impact
    be stated in student-minutes. `asthma` ({"7B": 2}) is the number of children
    with asthma per class, added to any counts in the timetable itself.

    Moves are chosen greedily, worst air first: each over-limit period takes the
    cleanest free slot of its own class. That is a sensible plan, not a
    guaranteed best one.
    """
    thresholds = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    margin = thresholds["forecast_margin"]
    capacity = thresholds["max_outdoor_classes"] or usual_outdoor_classes(periods)
    today = [p for p in periods if p.day == DAYS[day.weekday()]]
    today.sort(key=lambda p: (p.klass, p.start))
    asthma = {str(k).upper(): int(v) for k, v in (asthma or {}).items()}
    for p in periods:
        asthma[p.klass] = max(asthma.get(p.klass, 0), p.asthma)

    # First decide which outdoor periods are over the limit.
    facts = {}
    for period in today:
        if not period.outdoor:
            continue
        aqi = period_aqi(period, hourly)
        in_progress = now is not None and period.start <= now < period.end
        if in_progress and current_aqi is not None:
            aqi = current_aqi if aqi is None else max(aqi, current_aqi)
        limit = limit_for(period.klass, thresholds, asthma)
        when = _when(period, now)
        name = "past" if when == "past" else "unknown" if aqi is None else "ok" if aqi <= limit else "over"
        facts[period] = {"aqi": aqi, "limit": limit, "when": when, "in_progress": in_progress, "name": name,
                         "swap": None, "reason": None,
                         "for_asthma": limit < limit_for(period.klass, thresholds)}

    # Then place them, worst air first, without overfilling the ground or double-booking a teacher.
    outside = [(p.start, p.end, p.klass, p.teacher) for p, f in facts.items() if f["name"] in ("ok", "unknown")]
    slots = {p: (p.start, p.end) for p in today}   # where each period sits as the plan is built
    taken = set()  # indoor periods already used for a swap
    over = [p for p, f in facts.items() if f["name"] == "over"]
    for period in sorted(over, key=lambda p: (facts[p]["limit"] - facts[p]["aqi"], p.klass, p.start)):
        f = facts[period]
        swap = None
        if period.klass == WHOLE_SCHOOL:
            f["reason"] = "whole_school"
        elif f["in_progress"]:
            f["reason"] = "in_progress"
        else:
            swap, f["reason"] = _find_swap(period, today, hourly, f["limit"] - margin, taken, now,
                                           outside, capacity, slots, thresholds)
        if swap:
            taken.add(swap)
            outside.append((swap.start, swap.end, period.klass, period.teacher))
            slots[period], slots[swap] = slots[swap], slots[period]
            f["name"], f["swap"] = "swap", swap
        else:
            f["name"] = "indoors"

    # Finally the day as a whole. A class can pass every period and still spend too long outdoors
    # in poor air, so its heaviest remaining outdoor period is held indoors until the day fits.
    def load(klass, planned):
        """AQI-minutes of outdoor activity for a class, as timetabled or under the plan."""
        total = 0
        for p, f in facts.items():
            if p.klass not in (klass, WHOLE_SCHOOL) or f["aqi"] is None:
                continue
            if not planned or f["name"] in ("ok", "past"):
                total += p.duration * f["aqi"]
            elif f["name"] == "swap":
                total += p.duration * period_aqi(f["swap"], hourly)
        return total

    classes = sorted({p.klass for p in today if p.klass != WHOLE_SCHOOL},
                     key=lambda k: (grade_of(k) if grade_of(k) is not None else 99, k))
    allowance = {k: thresholds["daily_outdoor_minutes"] * limit_for(k, thresholds, asthma) for k in classes}
    if thresholds["daily_outdoor_minutes"]:
        for klass in classes:
            while load(klass, True) > allowance[klass]:
                own = [p for p, f in facts.items()
                       if p.klass == klass and f["name"] in ("ok", "swap") and not f["in_progress"]]
                if not own:
                    break   # only whole-school events are left, and those cannot be cut for one class
                heaviest = max(own, key=lambda p: (period_aqi(facts[p]["swap"] or p, hourly), p.start))
                f = facts[heaviest]
                slot = f["swap"] or heaviest
                outside.remove((slot.start, slot.end, klass, heaviest.teacher))
                if f["swap"]:
                    taken.discard(f["swap"])
                    slots[heaviest], slots[f["swap"]] = slots[f["swap"]], slots[heaviest]
                # A period that had been moved out of bad air is over its limit where it started.
                f.update(name="indoors", reason="daily_budget_after_move" if f["swap"] else "daily_budget", swap=None)

        # Cutting a moved period frees its slot on the ground. Offer it to a class that was turned
        # away for lack of space, as long as that keeps the class within its own allowance.
        for period in sorted((p for p, f in facts.items() if f["reason"] == "ground_full"),
                             key=lambda p: (facts[p]["limit"] - facts[p]["aqi"], p.klass, p.start)):
            f = facts[period]
            swap, reason = _find_swap(period, today, hourly, f["limit"] - margin, taken, now,
                                      outside, capacity, slots, thresholds)
            if swap and load(period.klass, True) + period.duration * period_aqi(swap, hourly) <= allowance[period.klass]:
                taken.add(swap)
                outside.append((swap.start, swap.end, period.klass, period.teacher))
                slots[period], slots[swap] = slots[swap], slots[period]
                f.update(name="swap", swap=swap, reason=None)

    actions, outcome = [], {}
    for period, f in facts.items():
        action = {"class": period.klass, **_describe(period, f["aqi"]), "limit": f["limit"],
                  "when": f["when"], "action": f["name"]}
        if f["swap"]:
            action["swap_with"] = _describe(f["swap"], period_aqi(f["swap"], hourly))
        if f["name"] == "indoors":
            action["reason"] = f["reason"]
        if f["for_asthma"]:
            action["asthma"] = max(asthma.values()) if period.klass == WHOLE_SCHOOL else asthma[period.klass]
        if f["name"] == "ok":
            # Close to the limit on a forecast: worth checking the live reading before going out.
            action["borderline"] = not f["in_progress"] and f["aqi"] > f["limit"] - margin
            action["sensitive"] = f["aqi"] > thresholds["sensitive_limit"]
        outcome[period] = (f["name"], f["aqi"], f["swap"])
        actions.append(action)

    actions.sort(key=lambda a: (a["start"], a["class"]))
    changes = [a for a in actions if a["action"] in ("swap", "indoors")]
    school_hours = _school_hours(today, hourly)

    plan = {
        "date": day.isoformat(),
        "weekday": day.strftime("%A"),
        "is_today": now is not None,
        "is_school_day": bool(today),
        "has_outdoor": any(p.outdoor for p in today),
        "verdict": _verdict(today, changes),
        "headline": _headline(today, changes, now is not None),
        "peak_aqi": max((h["aqi"] for h in school_hours), default=None),
        "hours": school_hours,
        "cleanest_hour": min(school_hours, key=lambda h: h["aqi"], default=None),
        "worst_hour": max(school_hours, key=lambda h: h["aqi"], default=None),
        "actions": actions,
        "changes": len(changes),
        "borderline": sum(1 for a in actions if a.get("borderline")),
        "sensitive": any(a.get("sensitive") for a in actions),
        "impact": _impact(facts, sizes),
        "load": [{"class": k, "allowance": allowance[k], "timetabled": load(k, False), "planned": load(k, True),
                  "percent_timetabled": round(100 * load(k, False) / allowance[k]),
                  "percent_planned": round(100 * load(k, True) / allowance[k])}
                 for k in classes if allowance[k] and load(k, False)],
        "grid": _grid(today, hourly, outcome),
        "thresholds": thresholds,
        "max_outdoor_classes": capacity,
        "has_teachers": any(p.teacher for p in today),
    }
    if now is not None and current_aqi is not None:
        plan["live"] = live_advice(current_aqi, now, thresholds)
    return plan


def _impact(facts, sizes):
    """What the plan achieves, or None when it changes nothing.

    Counts outdoor activity (running, PT, standing in assembly) in air over the
    school's limit. Moving a class indoors does not give it clean air, so this
    measures outdoor exertion avoided, not exposure removed.
    """
    changed = {p: f for p, f in facts.items() if f["name"] in ("swap", "indoors")}
    if not changed:
        return None
    over = {p: f for p, f in changed.items() if f["aqi"] > f["limit"]}
    impact = {
        "minutes_over_limit_avoided": sum(p.duration for p in over),
        "outdoor_minutes_kept": sum(p.duration for p, f in facts.items() if f["name"] in ("ok", "swap")),
        "worst_aqi_avoided": max((f["aqi"] for f in over.values()), default=None),
        "moved": sum(1 for f in changed.values() if f["name"] == "swap"),
        "held_indoors": sum(1 for f in changed.values() if f["name"] == "indoors"),
        "held_for_daily_allowance": sum(1 for f in changed.values() if (f["reason"] or "").startswith("daily_budget")),
    }
    if sizes and sizes.get("school") and sizes.get("per_class"):
        heads = lambda p: sizes["school"] if p.klass == WHOLE_SCHOOL else sizes["per_class"]
        impact["student_minutes_avoided"] = sum(p.duration * int(heads(p)) for p in over)
    return impact


def _grid(today, hourly, outcome):
    """The day's timetable per class, as scheduled and under the plan, for drawing."""
    if not today:
        return None
    moved_to = {}  # period -> period whose slot it takes under the plan
    for period, (name, _, swap) in outcome.items():
        if name == "swap":
            moved_to[period], moved_to[swap] = swap, period
    rows = {}
    for period in today:
        target = moved_to.get(period, period)
        name = outcome.get(period, (None,))[0]
        if period in moved_to:
            change = "moved" if period.outdoor else "moved_in"
        elif name == "indoors":
            change = "indoors"
        else:
            change = None
        rows.setdefault(period.klass, []).append({
            "activity": period.activity,
            "outdoor": period.outdoor,
            "start": period.start, "end": period.end,
            "aqi": outcome[period][1] if period in outcome else period_aqi(period, hourly),
            "after_start": target.start, "after_end": target.end,
            "after_aqi": period_aqi(target, hourly),
            "change": change,
            "past": name == "past",
        })
    order = sorted(rows, key=lambda k: (k != WHOLE_SCHOOL, grade_of(k) if grade_of(k) is not None else 99, k))
    return {
        "start": min(p.start for p in today) // 60 * 60,
        "end": -(-max(p.end for p in today) // 60) * 60,
        "rows": [{"class": k, "periods": sorted(rows[k], key=lambda x: x["start"])} for k in order],
    }


def live_advice(aqi, now, thresholds):
    """Can students be outside right now?"""
    primary, senior = thresholds["primary_limit"], thresholds["senior_limit"]
    cut = thresholds["primary_max_grade"]
    if aqi <= min(primary, senior):
        level, text = "ok", "Students can be outside right now."
    elif aqi > max(primary, senior):
        level, text = "none", "Keep all students indoors right now."
    elif primary < senior:
        level, text = "some", f"Keep classes up to {cut} indoors right now. Older classes can be outside."
    else:
        level, text = "some", f"Keep classes above {cut} indoors right now. Younger classes can be outside."
    return {"time": fmt_time(now), "aqi": aqi, "category": category(aqi), "level": level, "advice": text}


def _when(period, now):
    if now is None:
        return "planned"
    if period.end <= now:
        return "past"
    return "now" if period.start <= now else "later"


def _teacher_busy(teacher, start, end, slots, ignore):
    """Is this teacher already teaching another period that overlaps start-end?"""
    return bool(teacher) and any(q.teacher == teacher and q not in ignore and s < end and start < e
                                 for q, (s, e) in slots.items())


BLOCKERS = ["ground_full", "teacher_busy", "last_period", "back_to_back"]


def _find_swap(period, today, hourly, ceiling, taken, now, outside, capacity, slots, rules):
    """The cleanest usable slot for an over-limit period, and why there is none if there isn't.

    A usable slot is a same-length indoor lesson of the same class, at or under `ceiling`,
    where the ground has room and neither teacher is needed elsewhere.
    """
    best, best_aqi, blocked = None, None, set()
    last_start = max(p.start for p in today if p.klass == period.klass)
    for other in today:
        if other.klass != period.klass or other.outdoor or other in taken:
            continue
        if other.duration != period.duration:
            continue
        if now is not None and other.start <= now:
            continue
        aqi = period_aqi(other, hourly)
        if aqi is None or aqi > ceiling:
            continue
        # The air is clean enough here. What is left is space and people.
        if rules["avoid_last_period"] and other.start == last_start:
            blocked.add("last_period")
            continue
        sharing = [o for o in outside if o[0] < other.end and other.start < o[1]]
        if len(sharing) >= capacity or any(o[2] == WHOLE_SCHOOL for o in sharing):
            blocked.add("ground_full")
            continue
        pair = {period, other}
        if _teacher_busy(period.teacher, other.start, other.end, slots, pair) or \
                _teacher_busy(other.teacher, period.start, period.end, slots, pair):
            blocked.add("teacher_busy")
            continue
        if rules["avoid_back_to_back"] and period.teacher and any(
                o[3] == period.teacher and (o[1] == other.start or o[0] == other.end) for o in outside):
            blocked.add("back_to_back")
            continue
        if best is None or aqi < best_aqi:
            best, best_aqi = other, aqi
    if best:
        return best, None
    return None, next((b for b in BLOCKERS if b in blocked), "no_clean_slot")


def _school_hours(today, hourly):
    if not today:
        return []
    first = min(p.start for p in today) // 60
    last = (max(p.end for p in today) - 1) // 60
    return [
        {"hour": h, "aqi": hourly[h], "category": category(hourly[h])}
        for h in range(first, last + 1) if h in hourly
    ]


def _verdict(today, changes):
    if not today:
        return "no_school"
    if not changes:
        return "clear"
    if all(c["action"] == "swap" for c in changes):
        return "reschedule"
    return "indoors"


def _headline(today, changes, is_today):
    if not today:
        return "No classes are scheduled on this day."
    if not changes:
        if is_today:
            return "Air is within your limits for the rest of today."
        return "Air is within your limits. No changes needed."
    swaps = sum(1 for c in changes if c["action"] == "swap")
    indoors = len(changes) - swaps
    parts = []
    if swaps:
        parts.append(f"Move {swaps} {'period' if swaps == 1 else 'periods'} to cleaner hours.")
    if indoors:
        parts.append(f"Hold {indoors} {'activity' if indoors == 1 else 'activities'} indoors.")
    return " ".join(parts)


def has_periods_left(periods, day, now):
    return any(p.day == DAYS[day.weekday()] and p.end > now for p in periods)


def next_school_day(periods, after):
    """First date strictly after `after` that has periods in the timetable."""
    school_days = {p.day for p in periods}
    for offset in range(1, 8):
        candidate = after + timedelta(days=offset)
        if DAYS[candidate.weekday()] in school_days:
            return candidate
    return after + timedelta(days=1)
