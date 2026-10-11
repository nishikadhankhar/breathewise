"""Lambda entry point. Handles the HTTP API and the nightly EventBridge run.

Routes:
  GET  /               the web page
  GET  /api/schools    stored schools
  GET  /api/plan       ?school=<id>&date=YYYY-MM-DD   plan for a stored school
  POST /api/plan       {csv, lat, lon, name, date, thresholds, asthma}   plan for an uploaded timetable (not stored)
  GET  /api/backtest   ?school=<id>                   last winter: this plan against two simpler rules
  POST /api/backtest   {csv, lat, lon, thresholds, asthma}
  POST /api/settings/read   {school, passcode}                      a stored school's settings (staff only)
  POST /api/settings/save   {school, passcode, settings, csv}       change them (staff only)

Besides web requests it handles two scheduled runs: the evening one sends the next school
day's plan and saves it, and the morning one ({"run": "morning"}) sends only what has changed.

Without a date the plan is for today while classes are still to come (using
the current AQI), and for the next school day after that.
"""

import base64
import hmac
import json
import os
import time
from datetime import date
from pathlib import Path

from . import notify, store
from .backtest import backtest, last_winter
from .diff import compare, snapshot
from .forecast import FORECAST_DAYS, ForecastError, fetch_air
from .notice import write_notice
from .planner import DEFAULT_THRESHOLDS, build_plan, has_periods_left, next_school_day
from .timetable import TimetableError, read_timetable

WEB_PAGE = Path(__file__).resolve().parent.parent / "web" / "index.html"
MAX_BODY = 200_000


class BadRequest(Exception):
    pass


class Forbidden(Exception):
    pass


def lambda_handler(event, context=None):
    if "requestContext" not in event:
        # EventBridge sends no date. A direct invoke may pass {"date": "2025-11-18"} to send that day's plan.
        return morning() if event.get("run") == "morning" else nightly(event.get("date"))
    method = event["requestContext"]["http"]["method"]
    path = event.get("rawPath", "/")
    try:
        if method == "GET" and path == "/":
            return _reply(200, WEB_PAGE.read_text(encoding="utf-8"), "text/html; charset=utf-8")
        if method == "GET" and path == "/api/schools":
            return _json(200, {"schools": store.list_schools()})
        if method == "GET" and path == "/api/plan":
            return _json(200, plan_stored(event.get("queryStringParameters") or {}))
        if method == "POST" and path == "/api/plan":
            meta, csv_text, date_text = uploaded_school(_body(event))
            return _json(200, make_plan(meta, csv_text, date_text))
        if method == "GET" and path == "/api/backtest":
            meta, csv_text = store.get_school((event.get("queryStringParameters") or {}).get("school", ""))
            return _json(200, make_backtest(meta, csv_text))
        if method == "POST" and path == "/api/settings/read":
            return _json(200, settings_read(_body(event)))
        if method == "POST" and path == "/api/settings/save":
            return _json(200, settings_save(_body(event)))
        if method == "POST" and path == "/api/backtest":
            meta, csv_text, _ = uploaded_school(_body(event))
            return _json(200, make_backtest(meta, csv_text))
        return _json(404, {"error": "Not found."})
    except (BadRequest, TimetableError) as exc:
        return _json(400, {"error": str(exc)})
    except Forbidden as exc:
        return _json(403, {"error": str(exc)})
    except store.SchoolNotFound:
        return _json(404, {"error": "That school is not set up."})
    except ForecastError as exc:
        return _json(502, {"error": str(exc)})


def plan_stored(params):
    school_id = params.get("school", "")
    meta, csv_text = store.get_school(school_id)
    plan = make_plan(meta, csv_text, params.get("date"))
    # If staff were sent a plan for this day, show what the newer air data has changed.
    saved = store.load_plan(school_id, plan["date"])
    if saved:
        plan["since_last_plan"] = {"made_at": saved["made_at"], "changes": compare(saved, snapshot(plan))}
    return plan


def _staff_only(body):
    """Settings can only be changed with the passcode chosen when the app was deployed."""
    expected = os.environ.get("ADMIN_PASSCODE", "")
    if not expected:
        raise Forbidden("Changing settings from the web is not switched on for this site.")
    given = body.get("passcode")
    if not isinstance(given, str) or not hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8")):
        time.sleep(1)   # slows down guessing
        raise Forbidden("That passcode is not right.")


def settings_read(body):
    _staff_only(body)
    meta, csv_text = store.get_school(str(body.get("school", "")))
    periods, _ = read_timetable(csv_text)
    return {"settings": {**meta, "thresholds": {**DEFAULT_THRESHOLDS, **(meta.get("thresholds") or {})}},
            "periods": len(periods), "has_teachers": any(p.teacher for p in periods)}


def settings_save(body):
    _staff_only(body)
    school_id = str(body.get("school", ""))
    meta, _ = store.get_school(school_id)
    new = body.get("settings")
    if not isinstance(new, dict):
        raise BadRequest("No settings were sent.")
    if "thresholds" in new:
        meta["thresholds"] = {**(meta.get("thresholds") or {}), **_thresholds(new["thresholds"])}
    if "asthma" in new:
        meta["asthma"] = _asthma(new["asthma"]) or {}
    if "students" in new:
        sizes = new["students"] or {}
        try:
            meta["students"] = {**(meta.get("students") or {}),
                                **{k: int(sizes[k]) for k in ("school", "per_class") if sizes.get(k) not in (None, "")}}
        except (TypeError, ValueError, AttributeError):
            raise BadRequest("Student numbers must be whole numbers.") from None
        if any(not 0 <= v <= 100000 for v in meta["students"].values()):
            raise BadRequest("A student number is out of range.")
    if new.get("name"):
        meta["name"] = str(new["name"])[:80]
    csv_text = body.get("csv")
    if csv_text is not None:
        if not isinstance(csv_text, str):
            raise BadRequest("The timetable must be CSV text.")
        read_timetable(csv_text)   # refuse a timetable we cannot read
    store.save_school(school_id, meta, csv_text)
    _backtests.clear()
    return {"saved": True, "settings": meta}


def uploaded_school(body):
    """Check an upload and return (school settings, timetable CSV, date or None)."""
    try:
        lat, lon = float(body["lat"]), float(body["lon"])
    except (KeyError, TypeError, ValueError):
        raise BadRequest("Choose where the school is first.") from None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise BadRequest("Latitude or longitude is out of range.")
    if not isinstance(body.get("csv"), str) or not body["csv"].strip():
        raise BadRequest("A timetable CSV is required.")
    meta = {"name": str(body.get("name") or "Your school")[:80], "lat": lat, "lon": lon,
            "thresholds": _thresholds(body.get("thresholds")), "asthma": _asthma(body.get("asthma"))}
    return meta, body["csv"], body.get("date")


def _asthma(raw):
    """{"7B": 2}: children with asthma per class. Counts only, never names."""
    if raw in (None, ""):
        return None
    if not isinstance(raw, dict) or len(raw) > 200:
        raise BadRequest("Asthma counts must look like 7B:2, 3A:1.")
    clean = {}
    for klass, count in raw.items():
        try:
            count = int(count)
        except (TypeError, ValueError):
            raise BadRequest("Asthma counts must be whole numbers, like 7B:2.") from None
        if not 0 <= count <= 500:
            raise BadRequest("An asthma count is out of range.")
        clean[str(klass).strip().upper()[:20]] = count
    return clean


def _thresholds(raw):
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise BadRequest("Limits must be an object.")
    clean = {}
    for key in DEFAULT_THRESHOLDS:
        if raw.get(key) in (None, ""):
            continue
        try:
            value = int(raw[key])
        except (TypeError, ValueError):
            raise BadRequest("Limits must be whole numbers.") from None
        top = 12 if key == "primary_max_grade" else 500
        if not 0 <= value <= top:
            raise BadRequest(f"A limit is out of range (0 to {top}).")
        clean[key] = value
    return clean


def make_plan(meta, csv_text, date_text=None, planning_ahead=False):
    """`planning_ahead` skips today, for the evening run that plans tomorrow."""
    periods, notes = read_timetable(csv_text)
    tz = meta.get("timezone", "auto")
    air = fetch_air(meta["lat"], meta["lon"], None, tz)  # today and the next few days
    local = air.local_now()
    now = local.hour * 60 + local.minute
    if date_text:
        try:
            day = date.fromisoformat(date_text)
        except (TypeError, ValueError):
            raise BadRequest("Date must look like 2026-11-18.") from None
        if day.isoformat() not in air.by_date:
            # Outside the forecast window (a past day, usually): fetch that day and the ones after it.
            air = fetch_air(meta["lat"], meta["lon"], day, tz, days=FORECAST_DAYS)
    else:
        day = local.date()
        if planning_ahead or not has_periods_left(periods, day, now):
            day = next_school_day(periods, day)

    is_today = day == local.date()
    thresholds, sizes = meta.get("thresholds"), meta.get("students")
    plan = build_plan(
        periods, air.hourly(day), day, thresholds,
        now=now if is_today else None,
        current_aqi=air.current_aqi if is_today else None,
        sizes=sizes,
        asthma=meta.get("asthma"),
    )
    plan["school"] = meta["name"]
    plan["timezone"] = air.timezone
    # "archive" is the air model's record of a past day, which is better informed than the
    # forecast a school would have had the evening before.
    plan["data_kind"] = "live" if is_today else "archive" if day < local.date() else "forecast"
    plan["timetable_notes"] = notes
    plan["week"] = week_ahead(periods, air, thresholds, plan, meta.get("asthma"))
    plan["notice"] = write_notice(meta["name"], plan)
    return plan


_backtests = {}


def make_backtest(meta, csv_text):
    """Last winter under three rules. Slow to fetch (three months of hourly air), so kept in memory."""
    key = json.dumps([meta["lat"], meta["lon"], meta.get("thresholds"), meta.get("asthma"), csv_text], sort_keys=True)
    if key not in _backtests:
        periods, _ = read_timetable(csv_text)
        tz = meta.get("timezone", "auto")
        today = fetch_air(meta["lat"], meta["lon"], None, tz).local_now().date()
        start, end = last_winter(today)
        air = fetch_air(meta["lat"], meta["lon"], start, tz, days=(end - start).days + 1)
        result = backtest(periods, air.by_date, meta.get("thresholds"), meta.get("asthma"))
        result.update({"from": start.isoformat(), "to": end.isoformat(), "school": meta["name"]})
        if len(_backtests) > 20:
            _backtests.clear()
        _backtests[key] = result
    return _backtests[key]


def week_ahead(periods, air, thresholds, selected, asthma=None):
    """One line per school day we have air data for: does it need changes?

    Also marks each change on the selected day with how many of the other days need the
    same one, so a school can make it a standing change instead of acting on it daily.
    """
    key = lambda a: (a["class"], a["activity"], a["start"])
    week, repeats = [], {}
    for stamp in sorted(air.by_date):
        day = date.fromisoformat(stamp)
        plan = selected if stamp == selected["date"] else build_plan(periods, air.by_date[stamp], day, thresholds, asthma=asthma)
        if not plan["is_school_day"]:
            continue
        week.append({"date": stamp, "verdict": plan["verdict"], "changes": plan["changes"],
                     "peak_aqi": plan["peak_aqi"]})
        if plan is not selected:
            for action in plan["actions"]:
                if action["action"] in ("swap", "indoors"):
                    repeats[key(action)] = repeats.get(key(action), 0) + 1
    for action in selected["actions"]:
        if action["action"] in ("swap", "indoors"):
            action["also_on"] = repeats.get(key(action), 0)
    selected["other_days"] = len(week) - 1 if selected["is_school_day"] else len(week)
    return week


def nightly(date_text=None):
    """Plan the next school day (or `date_text`) for every stored school and send it to staff."""
    sent = []
    for school in store.list_schools():
        try:
            meta, csv_text = store.get_school(school["id"])
            plan = make_plan(meta, csv_text, date_text, planning_ahead=True)
            if not date_text:
                store.save_plan(school["id"], plan["date"], snapshot(plan))
            if not plan["changes"] and not date_text:
                # No news is good news: staff only hear from us on days that need a change.
                sent.append({"school": school["id"], "date": plan["date"], "skipped": "no changes needed"})
                continue
            sent.append({"school": school["id"], "date": plan["date"],
                         "sent_to": notify.send(school["id"], meta["name"], plan, plan["notice"])})
        except Exception as exc:  # one school's failure must not stop the rest
            print(f"Nightly plan failed for {school['id']}: {exc}")
            sent.append({"school": school["id"], "error": str(exc)})
    return {"sent": sent}


def morning():
    """Re-plan today on the morning's air data and send staff only what differs from last night."""
    sent = []
    for school in store.list_schools():
        try:
            meta, csv_text = store.get_school(school["id"])
            plan = make_plan(meta, csv_text)
            saved = store.load_plan(school["id"], plan["date"])
            changes = compare(saved, snapshot(plan)) if saved else []
            if not saved:
                sent.append({"school": school["id"], "date": plan["date"], "skipped": "no plan from last night to compare"})
            elif not changes:
                sent.append({"school": school["id"], "date": plan["date"], "skipped": "nothing has changed since last night"})
            else:
                sent.append({"school": school["id"], "date": plan["date"], "changes": len(changes),
                             "sent_to": notify.send_update(school["id"], meta["name"], plan, changes)})
        except Exception as exc:  # one school's failure must not stop the rest
            print(f"Morning update failed for {school['id']}: {exc}")
            sent.append({"school": school["id"], "error": str(exc)})
    return {"sent": sent}


def _body(event):
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8", "replace")
    if len(raw) > MAX_BODY:
        raise BadRequest("That timetable is too large.")
    try:
        body = json.loads(raw)
    except ValueError:
        raise BadRequest("Request body must be JSON.") from None
    if not isinstance(body, dict):
        raise BadRequest("Request body must be a JSON object.")
    return body


def _json(status, payload):
    return _reply(status, json.dumps(payload, ensure_ascii=False), "application/json; charset=utf-8")


def _reply(status, body, content_type):
    return {"statusCode": status, "headers": {"content-type": content_type}, "body": body}
