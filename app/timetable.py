"""Read a school's timetable CSV.

The expected shape is one row per period:

    day,class,start,end,activity,outdoor
    mon,7B,08:00,08:40,PT,yes

Two more columns are optional: `teacher` (so a move never double-books anyone)
and `asthma` (how many children in that class have asthma).

Real timetables are messier, so the reader also accepts common variations:
other column names (subject, grade, from/to, a single "time" range), 12-hour
times, day lists and ranges, and a missing "outdoor" column, which is then
guessed from the activity name. Everything it had to assume is returned as a
note so the school can check it.
"""

import csv
import io
import re
from dataclasses import dataclass

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
MAX_ROWS = 5000
WHOLE_SCHOOL = "ALL"

# Column names we accept, most specific first.
ALIASES = {
    "day": ["day", "weekday", "day of week", "days"],
    "class": ["class", "class section", "grade", "section", "standard", "std"],
    "start": ["start", "start time", "from", "begin", "begins", "starts"],
    "end": ["end", "end time", "to", "finish", "ends", "until"],
    "time": ["time", "timing", "timings", "slot", "period time"],
    "activity": ["activity", "subject", "lesson", "event", "period", "what"],
    "outdoor": ["outdoor", "outdoors", "outside", "is outdoor", "location", "venue", "where", "place"],
    # Optional. With a teacher column, nobody is double-booked by a move; an asthma column
    # gives the number of children with asthma in that class.
    "teacher": ["teacher", "staff", "faculty", "taught by"],
    "asthma": ["asthma", "asthma students", "children with asthma"],
}

OUTDOOR_WORDS = {"yes", "y", "true", "1", "outdoor", "outdoors", "outside", "ground", "playground",
                 "field", "court", "open", "lawn", "garden", "track"}
INDOOR_WORDS = {"no", "n", "false", "0", "indoor", "indoors", "inside", "classroom", "class", "room",
                "lab", "hall", "library", "auditorium", "gym"}

# Activities that usually happen outside, used only when the file does not say.
OUTDOOR_GUESS = re.compile(
    r"\b(pt|pe|p\.e\.?|p\.t\.?|physical|games?|sports?|assembly|football|soccer|cricket|hockey|"
    r"basketball|volleyball|kho|kabaddi|athletics?|running|race|yoga|playground|recess|"
    r"swimming|march|drill|ncc|scouts?|gardening|garden|field|outdoor|skating|tennis|throwball|handball)\b",
    re.IGNORECASE,
)


class TimetableError(ValueError):
    pass


@dataclass(frozen=True)
class Period:
    day: str
    klass: str
    start: int  # minutes from midnight
    end: int
    activity: str
    outdoor: bool
    teacher: str = ""
    asthma: int = 0

    @property
    def duration(self):
        return self.end - self.start


def fmt_time(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _norm(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _map_columns(fieldnames):
    headers = {_norm(h): h for h in fieldnames if h}
    columns = {}
    for field, names in ALIASES.items():
        for name in names:
            if name in headers and headers[name] not in columns.values():
                columns[field] = headers[name]
                break
    return columns, headers


def _parse_time(text, row, assumed_pm):
    """Minutes from midnight. Accepts 08:40, 8.40, 0840, 8:40 AM, 1pm."""
    raw = text.strip().lower().replace(".", ":") if text else ""
    match = re.fullmatch(r"(\d{1,2})(?::?(\d{2}))?(?::\d{2})?\s*(am|pm|a:m:|p:m:)?", raw)
    if not match:
        raise TimetableError(f"Row {row}: '{text}' is not a time like 08:40 or 8:40 AM.")
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    suffix = (match.group(3) or "").replace(":", "")
    if hour > 24 or minute > 59 or (suffix and not 1 <= hour <= 12):
        raise TimetableError(f"Row {row}: '{text}' is not a valid time of day.")
    if suffix == "pm" and hour != 12:
        hour += 12
    elif suffix == "am" and hour == 12:
        hour = 0
    elif not suffix and 1 <= hour <= 5:
        # No school runs at 1-5 in the morning, so a bare "1:10" means the afternoon.
        hour += 12
        assumed_pm.add(text.strip())
    return hour * 60 + minute


def _parse_days(text, row):
    """One day, a list (mon/wed/fri) or a range (mon-fri)."""
    raw = (text or "").strip().lower()
    if raw in ("daily", "everyday", "every day", "all", "all days"):
        return DAYS[:6], True
    span = re.fullmatch(r"([a-z]+)\.?\s*(?:-|–|to)\s*([a-z]+)\.?", raw)
    if span and span.group(1)[:3] in DAYS and span.group(2)[:3] in DAYS:
        first, last = DAYS.index(span.group(1)[:3]), DAYS.index(span.group(2)[:3])
        if first <= last:
            return DAYS[first:last + 1], False
    parts = [p for p in re.split(r"[\s,/&+]+", raw) if p and p != "and"]
    days = [p.rstrip(".")[:3] for p in parts]
    if not days or any(d not in DAYS for d in days):
        raise TimetableError(f"Row {row}: '{text}' is not a day of the week.")
    return days, False


def _read_rows(text):
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    first_line = text.split("\n", 1)[0]
    delimiter = max(",;\t|", key=first_line.count)
    return csv.DictReader(io.StringIO(text), delimiter=delimiter)


def read_timetable(text):
    """Return (periods, notes). Notes are plain sentences about what was assumed."""
    reader = _read_rows(text)
    columns, headers = _map_columns(reader.fieldnames or [])

    if sum(1 for h in headers if h[:3] in DAYS and len(h) <= 9) >= 3:
        raise TimetableError(
            "This looks like a grid timetable with days as columns. Please use one row per period "
            "with the columns day, class, start, end, activity, outdoor.")
    has_times = ("start" in columns and "end" in columns) or "time" in columns
    missing = [name for name in ("day", "activity") if name not in columns]
    if not has_times:
        missing.append("start and end")
    if missing:
        raise TimetableError(f"Timetable is missing column(s): {', '.join(missing)}.")

    periods, notes = [], []
    assumed_pm, guessed, unclear, daily = set(), set(), set(), False
    for row_no, raw in enumerate(reader, start=2):
        row = {field: (raw.get(col) or "").strip() for field, col in columns.items()}
        if not any(row.values()):
            continue
        if row_no > MAX_ROWS:
            raise TimetableError(f"Timetable is too long (more than {MAX_ROWS} rows).")
        if not row["activity"]:
            raise TimetableError(f"Row {row_no}: the activity is empty.")

        if "start" in columns and "end" in columns:
            start_text, end_text = row["start"], row["end"]
        else:
            pieces = re.split(r"\s*(?:-|–|—|to)\s*", row["time"])
            if len(pieces) != 2:
                raise TimetableError(f"Row {row_no}: '{row['time']}' is not a time range like 08:00-08:40.")
            start_text, end_text = pieces
        start = _parse_time(start_text, row_no, assumed_pm)
        end = _parse_time(end_text, row_no, assumed_pm)
        if end <= start:
            raise TimetableError(f"Row {row_no}: the period ends before it starts ({start_text} to {end_text}).")

        answer = _norm(row.get("outdoor", ""))
        if answer in OUTDOOR_WORDS or any(word in OUTDOOR_WORDS for word in answer.split()):
            outdoor = True
        elif answer in INDOOR_WORDS or any(word in INDOOR_WORDS for word in answer.split()):
            outdoor = False
        else:
            outdoor = bool(OUTDOOR_GUESS.search(row["activity"]))
            if answer:
                unclear.add(row["outdoor"])
            if outdoor:
                guessed.add(row["activity"])

        days, was_daily = _parse_days(row["day"], row_no)
        daily = daily or was_daily
        klass = re.sub(r"\s+", " ", row.get("class", "")).upper() or WHOLE_SCHOOL
        if klass in ("ALL", "WHOLE SCHOOL", "SCHOOL", "EVERYONE", "ALL CLASSES"):
            klass = WHOLE_SCHOOL
        teacher = re.sub(r"\s+", " ", row.get("teacher", "")).strip().title()
        try:
            asthma = max(0, int(row.get("asthma") or 0))
        except ValueError:
            raise TimetableError(f"Row {row_no}: the asthma column must be a number of children.") from None
        for day in days:
            periods.append(Period(day, klass, start, end, row["activity"], outdoor, teacher, asthma))

    if not periods:
        raise TimetableError("Timetable has no periods.")
    if not any(p.outdoor for p in periods):
        notes.append("No outdoor activities were found in this timetable, so there is nothing to check. "
                     "Add an 'outdoor' column with yes/no if that is wrong.")
    if guessed:
        names = ", ".join(sorted(guessed)[:8]) + ("…" if len(guessed) > 8 else "")
        notes.append(f"The file does not say which of these are outdoors, so they were treated as outdoor: {names}. "
                     "Add an 'outdoor' column with yes/no to be exact.")
    if unclear:
        notes.append(f"Could not read these 'outdoor' values: {', '.join(sorted(unclear)[:5])}. Use yes or no.")
    if assumed_pm:
        notes.append(f"Times without AM/PM between 1 and 5 were read as afternoon (for example {sorted(assumed_pm)[0]}).")
    if daily:
        notes.append("'Daily' was read as Monday to Saturday.")
    if "class" not in columns:
        notes.append("There is no class column, so every activity was treated as whole-school and cannot be moved.")
    overlaps = _overlaps(periods)
    if overlaps:
        notes.append(f"Some periods overlap for the same class ({overlaps[0]}). Check those rows.")
    return periods, notes


def parse_timetable(text):
    return read_timetable(text)[0]


def _overlaps(periods):
    found = []
    by_group = {}
    for p in periods:
        by_group.setdefault((p.day, p.klass), []).append(p)
    for (day, klass), group in by_group.items():
        if klass == WHOLE_SCHOOL:
            continue
        group.sort(key=lambda p: p.start)
        for a, b in zip(group, group[1:]):
            if b.start < a.end:
                found.append(f"{klass} on {day}: {a.activity} and {b.activity}")
    return found
