import unittest
from datetime import date

from app.aqi import category, indian_aqi
from app.forecast import ForecastError, parse_air
from app.notice import template_notice
from app import planner
from app.planner import grade_of, limit_for, live_advice, next_school_day, usual_outdoor_classes
from app.timetable import TimetableError, parse_timetable, read_timetable

MONDAY = date(2026, 11, 16)

# Most tests use a two-tier limit with no forecast margin and no cap on classes outdoors, so
# each one exercises a single rule. The margin and the cap have their own tests below.
PLAIN = {"primary_max_grade": 5, "primary_limit": 200, "senior_limit": 250,
         "forecast_margin": 0, "sensitive_limit": 100, "max_outdoor_classes": 9, "daily_outdoor_minutes": 0}


def build_plan(periods, hourly, day, thresholds=None, **kwargs):
    return planner.build_plan(periods, hourly, day, {**PLAIN, **(thresholds or {})}, **kwargs)

CSV = """day,class,start,end,activity,outdoor
mon,ALL,07:40,08:00,Assembly,yes
mon,7B,08:00,08:40,PT,yes
mon,7B,08:40,09:20,Maths,no
mon,7B,13:00,13:40,Library,no
mon,7B,13:40,14:40,Lab,no
mon,3A,13:00,13:40,Games,yes
mon,3A,08:00,08:40,Hindi,no
"""


def hourly(morning, afternoon):
    return {h: (morning if h < 12 else afternoon) for h in range(24)}


def by_activity(plan):
    return {a["activity"]: a for a in plan["actions"]}


class AqiTest(unittest.TestCase):
    def test_breakpoints(self):
        self.assertEqual(indian_aqi(30, 0), 50)
        self.assertEqual(indian_aqi(60, 0), 100)
        self.assertEqual(indian_aqi(90, 0), 200)
        self.assertEqual(indian_aqi(120, 0), 300)
        self.assertEqual(indian_aqi(250, 0), 400)
        self.assertEqual(indian_aqi(0, 250), 200)

    def test_worst_pollutant_wins_and_caps(self):
        self.assertEqual(indian_aqi(30, 350), 300)
        self.assertEqual(indian_aqi(900, 900), 500)
        self.assertIsNone(indian_aqi(None, None))
        self.assertEqual(indian_aqi(None, 100), 100)

    def test_category(self):
        self.assertEqual(category(50), "Good")
        self.assertEqual(category(201), "Poor")
        self.assertEqual(category(500), "Severe")


class TimetableTest(unittest.TestCase):
    def test_parses(self):
        periods = parse_timetable(CSV)
        self.assertEqual(len(periods), 7)
        self.assertTrue(periods[1].outdoor)
        self.assertEqual(periods[1].start, 480)

    def test_errors_name_the_row(self):
        with self.assertRaisesRegex(TimetableError, "missing column"):
            parse_timetable("day,class\nmon,7B\n")
        with self.assertRaisesRegex(TimetableError, "Row 2"):
            parse_timetable("day,class,start,end,activity,outdoor\nmon,7B,nine,10:00,PT,yes\n")
        with self.assertRaisesRegex(TimetableError, "ends before"):
            parse_timetable("day,class,start,end,activity,outdoor\nmon,7B,10:00,09:00,PT,yes\n")

    def test_other_column_names_and_12_hour_times(self):
        text = ("Day;Grade;From;To;Subject;Location\n"
                "Monday;VII-B;8:00 AM;8:40 AM;Physical Education;Main ground\n"
                "Monday;VII-B;12:30 PM;1:10 PM;Maths;Classroom 12\n")
        periods, notes = read_timetable(text)
        self.assertEqual([(p.start, p.end, p.outdoor) for p in periods], [(480, 520, True), (750, 790, False)])
        self.assertEqual(periods[0].klass, "VII-B")
        self.assertEqual(notes, [])

    def test_time_range_column_and_bare_afternoon_times(self):
        text = "day,class,time,activity,outdoor\ntue,5A,12.30-1.10,Games,yes\n"
        periods, notes = read_timetable(text)
        self.assertEqual((periods[0].start, periods[0].end), (750, 790))
        self.assertTrue(any("afternoon" in n for n in notes))

    def test_missing_outdoor_column_is_guessed_and_reported(self):
        text = "day,class,start,end,activity\nmon,6A,08:00,08:40,PT\nmon,6A,08:40,09:20,Maths\nmon,6A,09:20,10:00,Football practice\n"
        periods, notes = read_timetable(text)
        self.assertEqual([p.outdoor for p in periods], [True, False, True])
        self.assertTrue(any("treated as outdoor" in n for n in notes))

    def test_day_lists_and_ranges(self):
        text = "day,class,start,end,activity,outdoor\nMon-Fri,ALL,07:40,08:00,Assembly,yes\nmon/wed,4A,09:00,09:40,PT,yes\n"
        periods = parse_timetable(text)
        self.assertEqual(sum(1 for p in periods if p.activity == "Assembly"), 5)
        self.assertEqual([p.day for p in periods if p.activity == "PT"], ["mon", "wed"])

    def test_grid_timetable_is_rejected_with_help(self):
        with self.assertRaisesRegex(TimetableError, "one row per period"):
            parse_timetable("time,Monday,Tuesday,Wednesday\n08:00-08:40,PT,Maths,Art\n")

    def test_no_outdoor_activities_is_reported(self):
        _, notes = read_timetable("day,class,start,end,activity,outdoor\nmon,6A,08:00,08:40,Maths,no\n")
        self.assertTrue(any("No outdoor activities" in n for n in notes))


class PlannerTest(unittest.TestCase):
    def setUp(self):
        self.periods = parse_timetable(CSV)

    def test_grades(self):
        self.assertEqual(grade_of("7B"), 7)
        self.assertEqual(grade_of("VII-B"), 7)
        self.assertEqual(grade_of("CLASS IX A"), 9)
        self.assertEqual(grade_of("GRADE 3"), 3)
        self.assertEqual(grade_of("UKG"), 0)
        self.assertIsNone(grade_of("ROSE HOUSE"))

    def test_limits(self):
        self.assertEqual(limit_for("3A", PLAIN), 200)
        self.assertEqual(limit_for("7B", PLAIN), 250)
        self.assertEqual(limit_for("XI SCIENCE", PLAIN), 250)
        self.assertEqual(limit_for("ALL", PLAIN), 200)
        self.assertEqual(limit_for("ROSE HOUSE", PLAIN), 200)

    def test_clear_day(self):
        plan = build_plan(self.periods, hourly(120, 90), MONDAY)
        self.assertEqual(plan["verdict"], "clear")
        self.assertEqual(plan["changes"], 0)

    def test_bad_morning_swaps_pt_into_clean_afternoon(self):
        plan = build_plan(self.periods, hourly(320, 150), MONDAY)
        actions = by_activity(plan)
        self.assertEqual(actions["PT"]["action"], "swap")
        # Library is the same length; Lab is 60 minutes, so it is skipped.
        self.assertEqual(actions["PT"]["swap_with"]["activity"], "Library")
        self.assertEqual(actions["Assembly"]["action"], "indoors")
        self.assertEqual(actions["Games"]["action"], "ok")
        self.assertEqual(plan["verdict"], "indoors")
        self.assertEqual(plan["headline"], "Move 1 period to cleaner hours. Hold 1 activity indoors.")

    def test_no_clean_slot_means_indoors(self):
        plan = build_plan(self.periods, hourly(420, 410), MONDAY)
        self.assertTrue(all(a["action"] == "indoors" for a in plan["actions"]))

    def test_limit_depends_on_class(self):
        # 230 is over the primary limit (200) but under the senior limit (250).
        actions = by_activity(build_plan(self.periods, hourly(230, 230), MONDAY))
        self.assertEqual(actions["PT"]["action"], "ok")
        self.assertEqual(actions["Games"]["action"], "indoors")

    def test_schools_own_limits_are_used(self):
        strict = {"primary_limit": 100, "senior_limit": 100}
        plan = build_plan(self.periods, hourly(120, 120), MONDAY, strict)
        self.assertEqual(plan["changes"], 3)

    def test_missing_air_data_is_not_called_safe(self):
        actions = by_activity(build_plan(self.periods, {13: 90}, MONDAY))
        self.assertEqual(actions["PT"]["action"], "unknown")
        self.assertEqual(actions["Games"]["action"], "ok")

    def test_day_without_classes(self):
        plan = build_plan(self.periods, hourly(400, 400), date(2026, 11, 15))
        self.assertEqual(plan["verdict"], "no_school")

    def test_next_school_day_skips_days_off(self):
        self.assertEqual(next_school_day(self.periods, date(2026, 11, 13)), MONDAY)

    def test_notice_carries_the_plan(self):
        plan = build_plan(self.periods, hourly(320, 150), MONDAY)
        notice = template_notice("Test School", plan)
        self.assertIn("Monday, 16 November", notice["en"])
        self.assertIn("PT moves from 08:00–08:40 to 13:00–13:40", notice["en"])
        self.assertIn("सोमवार, 16 नवंबर", notice["hi"])
        self.assertIn("कक्षा 7B", notice["hi"])


class ImpactAndGridTest(unittest.TestCase):
    def setUp(self):
        self.periods = parse_timetable(CSV)
        # Assembly (20 min) is held indoors, PT (40 min) moves to 13:00, Games (40 min) is fine.
        self.plan = build_plan(self.periods, hourly(320, 150), MONDAY, sizes={"school": 105, "per_class": 35})

    def test_impact_counts_outdoor_activity_avoided_and_kept(self):
        impact = self.plan["impact"]
        self.assertEqual(impact["minutes_over_limit_avoided"], 60)     # assembly 20 + PT 40
        self.assertEqual(impact["outdoor_minutes_kept"], 80)           # PT in its new slot + Games
        self.assertEqual(impact["worst_aqi_avoided"], 320)
        self.assertEqual((impact["moved"], impact["held_indoors"]), (1, 1))
        self.assertEqual(impact["student_minutes_avoided"], 20 * 105 + 40 * 35)

    def test_impact_needs_sizes_for_student_minutes_and_is_absent_on_clear_days(self):
        self.assertNotIn("student_minutes_avoided", build_plan(self.periods, hourly(320, 150), MONDAY)["impact"])
        self.assertIsNone(build_plan(self.periods, hourly(90, 90), MONDAY)["impact"])

    def test_impact_when_nothing_can_stay_outdoors(self):
        impact = build_plan(self.periods, hourly(420, 410), MONDAY)["impact"]
        self.assertEqual(impact["minutes_over_limit_avoided"], 100)
        self.assertEqual(impact["outdoor_minutes_kept"], 0)

    def test_grid_shows_each_class_before_and_after(self):
        grid = self.plan["grid"]
        self.assertEqual((grid["start"], grid["end"]), (7 * 60, 15 * 60))
        self.assertEqual([r["class"] for r in grid["rows"]], ["ALL", "3A", "7B"])
        seven_b = {p["activity"]: p for p in grid["rows"][2]["periods"]}
        self.assertEqual((seven_b["PT"]["start"], seven_b["PT"]["after_start"], seven_b["PT"]["change"]), (480, 780, "moved"))
        self.assertEqual((seven_b["PT"]["aqi"], seven_b["PT"]["after_aqi"]), (320, 150))
        self.assertEqual((seven_b["Library"]["after_start"], seven_b["Library"]["change"]), (480, "moved_in"))
        self.assertEqual((seven_b["Maths"]["after_start"], seven_b["Maths"]["change"]), (520, None))
        self.assertEqual(grid["rows"][0]["periods"][0]["change"], "indoors")

    def test_week_ahead_lists_school_days_only(self):
        from types import SimpleNamespace
        from app.handler import week_ahead
        air = SimpleNamespace(by_date={"2026-11-15": hourly(400, 400),     # Sunday: no classes
                                       "2026-11-16": hourly(320, 150),     # Monday, the selected day
                                       "2026-11-23": hourly(90, 90)})      # next Monday
        week = week_ahead(self.periods, air, None, self.plan)
        self.assertEqual([(d["date"], d["verdict"], d["changes"]) for d in week],
                         [("2026-11-16", "indoors", 2), ("2026-11-23", "clear", 0)])

    def test_week_ahead_marks_changes_that_repeat(self):
        from types import SimpleNamespace
        from app.handler import week_ahead
        air = SimpleNamespace(by_date={"2026-11-16": hourly(320, 150), "2026-11-23": hourly(320, 150),
                                       "2026-11-30": hourly(90, 90)})
        week_ahead(self.periods, air, PLAIN, self.plan)
        repeats = {a["activity"]: a.get("also_on") for a in self.plan["actions"]}
        self.assertEqual((repeats["Assembly"], repeats["PT"], repeats["Games"]), (1, 1, None))
        self.assertEqual(self.plan["other_days"], 2)


class SafetyRulesTest(unittest.TestCase):
    """The rules added so the plan holds up in a real school: margin, ground capacity, asthma."""

    TWO_CLASSES = """day,class,start,end,activity,outdoor
mon,3A,08:00,08:40,Games,yes
mon,3A,13:00,13:40,Maths,no
mon,7B,08:00,08:40,PT,yes
mon,7B,13:00,13:40,Hindi,no
"""

    def test_period_spanning_two_hours_is_judged_on_the_worse_one(self):
        periods = parse_timetable("day,class,start,end,activity,outdoor\nmon,7B,10:40,11:10,Break,yes\n")
        action = build_plan(periods, {10: 302, 11: 150}, MONDAY)["actions"][0]
        self.assertEqual((action["aqi"], action["action"]), (302, "indoors"))

    def test_two_classes_are_not_moved_onto_the_ground_together(self):
        periods = parse_timetable(self.TWO_CLASSES)
        crowded = by_activity(build_plan(periods, hourly(320, 150), MONDAY))                       # no cap
        self.assertEqual((crowded["Games"]["action"], crowded["PT"]["action"]), ("swap", "swap"))
        capped = by_activity(build_plan(periods, hourly(320, 150), MONDAY, {"max_outdoor_classes": 1}))
        self.assertEqual(sorted([capped["Games"]["action"], capped["PT"]["action"]]), ["indoors", "swap"])

    def test_cap_defaults_to_what_the_timetable_already_does(self):
        periods = parse_timetable(self.TWO_CLASSES)
        self.assertEqual(usual_outdoor_classes(periods), 2)       # both classes are out at 08:00 as scheduled
        self.assertEqual(usual_outdoor_classes(parse_timetable(CSV)), 1)
        plan = build_plan(parse_timetable(CSV), hourly(320, 150), MONDAY, {"max_outdoor_classes": 0})
        self.assertEqual(plan["max_outdoor_classes"], 1)

    def test_a_class_is_not_moved_into_a_slot_already_used_outdoors(self):
        text = self.TWO_CLASSES.replace("mon,7B,08:00,08:40,PT,yes", "mon,7B,13:00,13:40,PT,yes") \
                               .replace("mon,7B,13:00,13:40,Hindi,no", "mon,7B,08:00,08:40,Hindi,no")
        plan = build_plan(parse_timetable(text), hourly(320, 150), MONDAY, {"max_outdoor_classes": 1})
        self.assertEqual(by_activity(plan)["Games"]["action"], "indoors")   # 13:00 is 7B's PT slot

    def test_worst_air_gets_the_clean_slot_first(self):
        # One clean slot, two candidates: the class in AQI 340 gets it, not the one in AQI 260.
        text = ("day,class,start,end,activity,outdoor\n"
                "mon,3A,09:00,09:40,Games,yes\nmon,3A,13:00,13:40,Maths,no\n"
                "mon,7B,08:00,08:40,PT,yes\nmon,7B,13:00,13:40,Hindi,no\n")
        air = {**hourly(150, 150), 8: 340, 9: 260}
        actions = by_activity(build_plan(parse_timetable(text), air, MONDAY, {"max_outdoor_classes": 1, "senior_limit": 200}))
        self.assertEqual((actions["PT"]["action"], actions["Games"]["action"]), ("swap", "indoors"))

    def test_forecast_margin_keeps_moves_clear_of_the_limit_and_flags_close_calls(self):
        periods = parse_timetable(CSV)
        # 13:00 is at 185, more than 30 under 7B's limit of 250, so PT moves there.
        self.assertEqual(by_activity(build_plan(periods, hourly(320, 185), MONDAY, {"forecast_margin": 30}))["PT"]["action"], "swap")
        # At 235 the slot is under the limit but inside the margin, so PT is not moved there.
        self.assertEqual(by_activity(build_plan(periods, hourly(320, 235), MONDAY, {"forecast_margin": 30}))["PT"]["action"], "indoors")
        self.assertEqual(by_activity(build_plan(periods, hourly(320, 235), MONDAY))["PT"]["action"], "swap")
        # Games at 185 against a limit of 200 goes ahead, but is flagged as a close call.
        plan = build_plan(periods, hourly(320, 185), MONDAY, {"forecast_margin": 30})
        self.assertTrue(by_activity(plan)["Games"]["borderline"])
        self.assertEqual(plan["borderline"], 1)
        self.assertFalse(by_activity(build_plan(periods, hourly(320, 150), MONDAY, {"forecast_margin": 30}))["Games"]["borderline"])

    def test_outdoor_activity_above_the_sensitive_limit_is_flagged(self):
        periods = parse_timetable(CSV)
        self.assertTrue(build_plan(periods, hourly(150, 150), MONDAY)["sensitive"])
        self.assertFalse(build_plan(periods, hourly(90, 90), MONDAY)["sensitive"])

    def test_defaults_use_one_cpcb_based_limit(self):
        plan = planner.build_plan(parse_timetable(CSV), hourly(230, 230), MONDAY)
        self.assertEqual({a["limit"] for a in plan["actions"]}, {200})
        self.assertEqual(plan["thresholds"]["forecast_margin"], 30)

    def test_notice_does_not_recommend_masks_or_call_indoors_safe(self):
        notice = template_notice("Test School", build_plan(parse_timetable(CSV), hourly(320, 150), MONDAY))
        for text in notice.values():
            self.assertNotIn("mask", text.lower())
            self.assertNotIn("मास्क", text)
        self.assertIn("held indoors as a light activity", notice["en"])


class LiveTest(unittest.TestCase):
    def setUp(self):
        self.periods = parse_timetable(CSV)

    def test_finished_periods_are_left_alone(self):
        # 09:00: assembly and PT are over, so the bad morning no longer needs action.
        plan = build_plan(self.periods, hourly(320, 150), MONDAY, now=9 * 60, current_aqi=320)
        actions = by_activity(plan)
        self.assertEqual(actions["Assembly"]["action"], "past")
        self.assertEqual(actions["PT"]["action"], "past")
        self.assertEqual(plan["changes"], 0)

    def test_period_in_progress_uses_current_aqi_and_cannot_be_moved(self):
        # Hourly data says fine, but the current reading has jumped over the limit.
        plan = build_plan(self.periods, hourly(150, 150), MONDAY, now=8 * 60 + 10, current_aqi=300)
        pt = by_activity(plan)["PT"]
        self.assertEqual((pt["action"], pt["when"], pt["aqi"]), ("indoors", "now", 300))

    def test_swap_never_uses_a_slot_that_has_started(self):
        # At noon, 3A's Hindi (08:00) is over, so the 13:00 Games can only move to the later Art slot.
        periods = parse_timetable(CSV + "mon,3A,15:00,15:40,Art,no\n")
        air = {**hourly(100, 100), 13: 260}
        games = by_activity(build_plan(periods, air, MONDAY, now=12 * 60, current_aqi=100))["Games"]
        self.assertEqual(games["action"], "swap")
        self.assertEqual(games["swap_with"]["activity"], "Art")
        # Planned the evening before, the same day would have used the earlier Hindi slot.
        planned = by_activity(build_plan(periods, air, MONDAY))["Games"]
        self.assertEqual(planned["swap_with"]["activity"], "Hindi")

    def test_live_advice_levels(self):
        self.assertEqual(live_advice(150, 600, PLAIN)["level"], "ok")
        some = live_advice(230, 600, PLAIN)
        self.assertEqual(some["level"], "some")
        self.assertIn("classes up to 5", some["advice"])
        self.assertEqual(live_advice(300, 600, PLAIN)["level"], "none")


class ForecastTest(unittest.TestCase):
    def test_parse(self):
        payload = {"utc_offset_seconds": 19800, "timezone": "Asia/Kolkata",
                   "current": {"pm2_5": 60, "pm10": 80},
                   "hourly": {"time": ["2026-11-16T08:00", "2026-11-16T09:00", "2026-11-17T08:00"],
                              "pm2_5": [120, None, 30], "pm10": [100, None, 20]}}
        air = parse_air(payload)
        self.assertEqual(air.by_date, {"2026-11-16": {8: 300}, "2026-11-17": {8: 50}})
        self.assertEqual(air.current_aqi, 100)
        self.assertEqual(air.hourly(MONDAY), {8: 300})
        with self.assertRaises(ForecastError):
            air.hourly(date(2026, 11, 20))

    def test_empty_raises(self):
        with self.assertRaises(ForecastError):
            parse_air({"hourly": {"time": [], "pm2_5": [], "pm10": []}})


if __name__ == "__main__":
    unittest.main()


class ConstraintTest(unittest.TestCase):
    """Moves must work for the people involved, not just for the air."""

    # 7B's PT (Rao) is in bad air at 08:00. Its clean indoor slots are 12:00 (Maths, Iyer) and 13:00 (Hindi, Sharma).
    BASE = ("day,class,start,end,activity,outdoor,teacher\n"
            "mon,7B,08:00,08:40,PT,yes,Mr Rao\n"
            "mon,7B,12:00,12:40,Maths,no,Ms Iyer\n"
            "mon,7B,13:00,13:40,Hindi,no,Ms Sharma\n")
    AIR = {**hourly(320, 150), 12: 160}   # 13:00 is the cleaner of the two slots

    def plan(self, extra="", **rules):
        return by_activity(build_plan(parse_timetable(self.BASE + extra), self.AIR, MONDAY, rules))

    def test_reads_teacher_and_asthma_columns(self):
        periods = parse_timetable("day,class,start,end,activity,outdoor,teacher,asthma\nmon,7B,08:00,08:40,PT,yes,  mr  rao ,3\n")
        self.assertEqual((periods[0].teacher, periods[0].asthma), ("Mr Rao", 3))
        with self.assertRaisesRegex(TimetableError, "asthma column"):
            parse_timetable("day,class,start,end,activity,outdoor,asthma\nmon,7B,08:00,08:40,PT,yes,some\n")

    def test_takes_the_cleanest_slot_when_nobody_is_in_the_way(self):
        self.assertEqual(self.plan()["PT"]["swap_with"]["start"], "13:00")

    def test_does_not_move_pt_when_its_teacher_is_teaching_another_class_then(self):
        # Rao has 9A indoors for theory at 13:00, so PT goes to 12:00 instead.
        pt = self.plan("mon,9A,13:00,13:40,Health theory,no,Mr Rao\n")["PT"]
        self.assertEqual(pt["swap_with"]["start"], "12:00")

    def test_does_not_send_the_displaced_teacher_into_a_clash(self):
        # Hindi would move to 08:00, but Sharma already teaches 9A then. Same for Iyer: nothing is usable.
        extra = "mon,9A,08:00,08:40,Hindi,no,Ms Sharma\nmon,9B,08:00,08:40,Maths,no,Ms Iyer\n"
        pt = self.plan(extra)["PT"]
        self.assertEqual((pt["action"], pt["reason"]), ("indoors", "teacher_busy"))

    def test_last_period_rule_is_optional(self):
        self.assertEqual(self.plan(avoid_last_period=1)["PT"]["swap_with"]["start"], "12:00")
        only_last = by_activity(build_plan(parse_timetable(self.BASE), {**hourly(320, 150), 12: 320}, MONDAY, {"avoid_last_period": 1}))
        self.assertEqual((only_last["PT"]["action"], only_last["PT"]["reason"]), ("indoors", "last_period"))

    def test_back_to_back_rule_is_optional(self):
        # Rao already has 9A outdoors 13:40-14:20, right after the 13:00 slot.
        extra = "mon,9A,13:40,14:20,Games,yes,Mr Rao\n"
        self.assertEqual(self.plan(extra)["PT"]["swap_with"]["start"], "13:00")
        self.assertEqual(self.plan(extra, avoid_back_to_back=1)["PT"]["swap_with"]["start"], "12:00")

    def test_reasons_for_holding_indoors(self):
        periods = parse_timetable(CSV)
        actions = by_activity(build_plan(periods, hourly(420, 410), MONDAY))
        self.assertEqual(actions["Assembly"]["reason"], "whole_school")
        self.assertEqual(actions["PT"]["reason"], "no_clean_slot")
        live = by_activity(build_plan(periods, hourly(150, 150), MONDAY, now=8 * 60 + 10, current_aqi=300))
        self.assertEqual(live["PT"]["reason"], "in_progress")
        full = by_activity(build_plan(parse_timetable(SafetyRulesTest.TWO_CLASSES), hourly(320, 150), MONDAY, {"max_outdoor_classes": 1}))
        self.assertIn("ground_full", [full["Games"].get("reason"), full["PT"].get("reason")])


class AsthmaTest(unittest.TestCase):
    def setUp(self):
        self.periods = parse_timetable(CSV)

    def test_class_with_asthma_uses_the_stricter_limit(self):
        # 180 is fine for 7B (limit 250) unless it has children with asthma (limit 150).
        without = by_activity(build_plan(self.periods, hourly(180, 120), MONDAY))["PT"]
        self.assertEqual((without["limit"], without["action"]), (250, "ok"))
        with_asthma = by_activity(build_plan(self.periods, hourly(180, 120), MONDAY, asthma={"7b": 2}))["PT"]
        self.assertEqual((with_asthma["limit"], with_asthma["action"], with_asthma["asthma"]), (150, "swap", 2))

    def test_whole_school_events_follow_the_strictest_class(self):
        assembly = by_activity(build_plan(self.periods, hourly(180, 120), MONDAY, asthma={"7B": 2}))["Assembly"]
        self.assertEqual((assembly["limit"], assembly["action"]), (150, "indoors"))
        self.assertEqual(by_activity(build_plan(self.periods, hourly(180, 120), MONDAY))["Assembly"]["limit"], 200)

    def test_counts_can_come_from_the_timetable(self):
        text = "day,class,start,end,activity,outdoor,asthma\nmon,7B,08:00,08:40,PT,yes,1\n"
        self.assertEqual(build_plan(parse_timetable(text), hourly(180, 120), MONDAY)["actions"][0]["limit"], 150)

    def test_asthma_limit_never_loosens_a_stricter_class_limit(self):
        plan = build_plan(self.periods, hourly(120, 120), MONDAY, {"primary_limit": 100, "asthma_limit": 150}, asthma={"3A": 1})
        self.assertEqual(by_activity(plan)["Games"]["limit"], 100)


class BacktestTest(unittest.TestCase):
    def test_last_winter(self):
        from app.backtest import last_winter
        self.assertEqual(last_winter(date(2026, 10, 10)), (date(2025, 11, 1), date(2026, 1, 31)))
        self.assertEqual(last_winter(date(2027, 1, 15)), (date(2025, 11, 1), date(2026, 1, 31)))   # this winter is not over
        self.assertEqual(last_winter(date(2027, 2, 1)), (date(2026, 11, 1), date(2027, 1, 31)))

    def test_three_rules_are_scored_on_the_same_days(self):
        from app.backtest import backtest
        periods = parse_timetable(CSV)   # Monday: assembly 20 min (07:40), PT 40 (08:00), Games 40 (13:00)
        air = {"2026-11-16": hourly(320, 150),   # bad morning, clean afternoon: the noon rule is right
               "2026-11-23": hourly(120, 120),   # clean all day: the noon rule cancels 60 minutes for nothing
               "2026-11-30": hourly(320, 320),   # bad all day: the noon rule sends Games out in bad air
               "2026-11-22": hourly(320, 320)}   # a Sunday: not counted
        result = backtest(periods, air, PLAIN)
        rows = {s["key"]: s for s in result["strategies"]}
        self.assertEqual((result["school_days"], result["days_with_changes"], result["days_noon_rule_wrong"]), (3, 2, 2))
        self.assertEqual(result["scheduled_outdoor_minutes"], 300)
        self.assertEqual(result["noon_rule_needless_minutes"], 60)
        self.assertEqual((rows["as_scheduled"]["over_limit_minutes"], rows["as_scheduled"]["outdoor_minutes"]), (160, 300))
        self.assertEqual((rows["noon_rule"]["over_limit_minutes"], rows["noon_rule"]["outdoor_minutes"]), (40, 120))
        # BreatheWise: day 1 keeps PT (moved) and Games, day 2 keeps everything, day 3 keeps nothing.
        self.assertEqual((rows["breathewise"]["over_limit_minutes"], rows["breathewise"]["outdoor_minutes"]), (0, 180))
        self.assertEqual(rows["breathewise"]["outdoor_kept_percent"], 60)


class DailyAllowanceTest(unittest.TestCase):
    """A class can pass every period and still have a bad day."""

    FOUR_PERIODS = ("day,class,start,end,activity,outdoor\n"
                    "mon,7B,08:00,08:40,PT,yes\nmon,7B,09:00,09:40,Games,yes\n"
                    "mon,7B,10:00,10:40,Athletics,yes\nmon,7B,11:00,11:40,Football,yes\n")

    def test_four_passing_periods_can_break_the_daily_allowance(self):
        periods = parse_timetable(self.FOUR_PERIODS)
        air = {8: 190, 9: 185, 10: 180, 11: 175}            # every period is under the limit of 200
        free = build_plan(periods, air, MONDAY, {"senior_limit": 200})
        self.assertEqual(free["changes"], 0)
        self.assertEqual(free["load"], [])
        # Allowance: 90 minutes at 200 = 18,000. Timetabled: 40 x (190+185+180+175) = 29,200.
        plan = build_plan(periods, air, MONDAY, {"senior_limit": 200, "daily_outdoor_minutes": 90})
        actions = by_activity(plan)
        self.assertEqual([actions[a]["action"] for a in ("PT", "Games", "Athletics", "Football")], ["indoors", "indoors", "ok", "ok"])
        self.assertEqual(actions["PT"]["reason"], "daily_budget")
        self.assertEqual(plan["load"], [{"class": "7B", "allowance": 18000, "timetabled": 29200, "planned": 14200,
                                         "percent_timetabled": 162, "percent_planned": 79}])
        self.assertEqual((plan["impact"]["minutes_over_limit_avoided"], plan["impact"]["held_for_daily_allowance"]), (0, 2))
        self.assertIsNone(plan["impact"]["worst_aqi_avoided"])

    def test_a_moved_period_counts_at_its_new_slot_and_frees_the_slot_if_cut(self):
        periods = parse_timetable(CSV)       # 7B: PT 08:00 moves to 13:00 (AQI 150)
        plan = build_plan(periods, hourly(320, 150), MONDAY, {"daily_outdoor_minutes": 90})
        row = next(r for r in plan["load"] if r["class"] == "7B")
        self.assertEqual(row["timetabled"], 20 * 320 + 40 * 320)     # assembly + PT in the bad morning
        self.assertEqual(row["planned"], 40 * 150)                   # assembly indoors, PT in clean air
        tight = build_plan(periods, hourly(320, 150), MONDAY, {"daily_outdoor_minutes": 20})   # 20 x 250 = 5,000
        pt = by_activity(tight)["PT"]
        self.assertEqual((pt["action"], pt["reason"]), ("indoors", "daily_budget_after_move"))
        library = next(p for r in tight["grid"]["rows"] if r["class"] == "7B" for p in r["periods"] if p["activity"] == "Library")
        self.assertIsNone(library["change"])                         # the swap was undone

    def test_whole_school_events_count_for_every_class_but_are_not_cut_for_one(self):
        periods = parse_timetable("day,class,start,end,activity,outdoor\nmon,ALL,08:00,10:00,Sports day,yes\nmon,7B,11:00,11:40,Maths,no\n")
        plan = build_plan(periods, hourly(190, 190), MONDAY, {"daily_outdoor_minutes": 90})
        self.assertEqual(plan["changes"], 0)
        self.assertEqual(plan["load"][0]["percent_planned"], round(100 * 120 * 190 / (90 * 250)))   # 7B's limit here is 250

    def test_a_slot_freed_by_the_allowance_goes_to_a_class_that_was_waiting(self):
        # One clean slot at 13:00 and room for one class. 7B (worse air) gets it first, then loses it to
        # its own daily allowance because of a long walk later on. 3A should then get the slot.
        text = ("day,class,start,end,activity,outdoor\n"
                "mon,3A,09:00,09:40,Games,yes\nmon,3A,13:00,13:40,Maths,no\n"
                "mon,7B,08:00,08:40,PT,yes\nmon,7B,13:00,13:40,Hindi,no\nmon,7B,14:00,15:20,Field trip walk,yes\n")
        air = {**hourly(150, 150), 8: 340, 9: 260, 14: 120, 15: 120}
        rules = {"max_outdoor_classes": 1, "senior_limit": 200, "daily_outdoor_minutes": 70}   # allowance 14,000
        actions = by_activity(build_plan(parse_timetable(text), air, MONDAY, rules))
        # 7B: walk 80 x 120 = 9,600, plus PT at 13:00 (40 x 150 = 6,000) = 15,600: over, and PT is in the worse air.
        self.assertEqual((actions["PT"]["action"], actions["PT"]["reason"]), ("indoors", "daily_budget_after_move"))
        self.assertEqual(actions["Field trip walk"]["action"], "ok")
        self.assertEqual(actions["Games"]["action"], "swap")
        self.assertEqual(actions["Games"]["swap_with"]["start"], "13:00")
