"""The AWS code paths, run against stand-in clients so they need no account.

These check that we call S3, SNS and Bedrock with the right shapes and read
their replies correctly. They do not prove the deployed stack works.
"""

import io
import json
import os
import sys
import types
import unittest
from datetime import date
from unittest import mock

from app import notice, notify, store
from app.handler import lambda_handler
from app.planner import build_plan
from app.timetable import parse_timetable

CSV = "day,class,start,end,activity,outdoor\nmon,7B,08:00,08:40,PT,yes\nmon,7B,13:00,13:40,Library,no\n"
SCHOOL = {"name": "Test School", "lat": 28.6, "lon": 77.2}
MONDAY = date(2026, 11, 16)


class FakeS3:
    objects = {"schools/test-school/school.json": json.dumps(SCHOOL), "schools/test-school/timetable.csv": CSV,
               "schools/broken/readme.txt": "no school.json here"}

    def get_paginator(self, name):
        pages = [{"CommonPrefixes": [{"Prefix": "schools/broken/"}, {"Prefix": "schools/test-school/"}]}]
        return types.SimpleNamespace(paginate=lambda **kw: pages)

    def __init__(self):
        self.objects = dict(FakeS3.objects)   # each test gets its own bucket

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[Key].encode("utf-8"))}

    def put_object(self, Bucket, Key, Body, **kwargs):
        self.objects[Key] = Body.decode("utf-8")


class FakeSNS:
    def __init__(self):
        self.sent = []

    def publish(self, **kwargs):
        self.sent.append(kwargs)


class FakeBedrock:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.reply, Exception):
            raise self.reply
        return {"output": {"message": {"content": [{"text": self.reply}]}}}


def fake_boto3(**clients):
    return mock.patch.dict(sys.modules, {"boto3": types.SimpleNamespace(client=lambda name, **kwargs: clients[name])})


def bad_day_plan():
    hourly = {h: (320 if h < 12 else 150) for h in range(24)}
    return build_plan(parse_timetable(CSV), hourly, MONDAY)


class StoreOnS3Test(unittest.TestCase):
    def test_lists_and_reads_schools_from_the_bucket(self):
        with fake_boto3(s3=FakeS3()), mock.patch.object(store, "BUCKET", "bucket"):
            self.assertEqual(store.list_schools(), [{"id": "test-school", "name": "Test School"}])
            meta, csv_text = store.get_school("test-school")
            self.assertEqual((meta["lat"], csv_text), (28.6, CSV))
            with self.assertRaises(store.SchoolNotFound):
                store.get_school("missing")
            with self.assertRaises(store.SchoolNotFound):
                store.get_school("../secrets")


class BedrockNoticeTest(unittest.TestCase):
    def setUp(self):
        notice._cache.clear()
        self.env = mock.patch.dict(os.environ, {"USE_BEDROCK": "1", "BEDROCK_MODEL_ID": "test-model"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_uses_the_model_and_caches_the_result(self):
        bedrock = FakeBedrock('```json\n{"en": "English notice", "hi": "हिंदी सूचना"}\n```')
        with fake_boto3(**{"bedrock-runtime": bedrock}):
            first = notice.write_notice("Test School", bad_day_plan())
            second = notice.write_notice("Test School", bad_day_plan())
        self.assertEqual(first, {"en": "English notice", "hi": "हिंदी सूचना", "source": "bedrock"})
        self.assertEqual(second, first)
        self.assertEqual(len(bedrock.calls), 1)
        call = bedrock.calls[0]
        self.assertEqual(call["modelId"], "test-model")
        facts = json.loads(call["messages"][0]["content"][0]["text"])
        self.assertEqual(facts["date_english"], "Monday, 16 November")
        self.assertEqual(facts["changes"][0]["swap_with"]["start"], "13:00")

    def test_falls_back_to_the_template_when_the_model_fails_or_rambles(self):
        for reply in (RuntimeError("AccessDeniedException"), "Sure! Here is your notice.", '{"en": "only english"}'):
            notice._cache.clear()
            with fake_boto3(**{"bedrock-runtime": FakeBedrock(reply)}), mock.patch("builtins.print"):
                result = notice.write_notice("Test School", bad_day_plan())
            self.assertEqual(result["source"], "template")
            self.assertIn("PT moves from 08:00–08:40 to 13:00–13:40", result["en"])

    def test_clear_day_does_not_call_the_model(self):
        bedrock = FakeBedrock("{}")
        plan = build_plan(parse_timetable(CSV), {h: 80 for h in range(24)}, MONDAY)
        with fake_boto3(**{"bedrock-runtime": bedrock}):
            self.assertEqual(notice.write_notice("Test School", plan)["source"], "template")
        self.assertEqual(bedrock.calls, [])


class SnsTest(unittest.TestCase):
    def test_publishes_the_plan_to_the_topic(self):
        sns = FakeSNS()
        plan = bad_day_plan()
        with fake_boto3(sns=sns), mock.patch.dict(os.environ, {"ALERT_TOPIC_ARN": "arn:topic"}):
            where = notify.send("test-school", "Test School", plan, notice.template_notice("Test School", plan))
        self.assertEqual(where, "sns")
        message = sns.sent[0]
        self.assertEqual(message["TopicArn"], "arn:topic")
        self.assertLessEqual(len(message["Subject"]), 100)
        self.assertIn("swap PT (08:00-08:40, AQI 320) with Library (13:00-13:40, AQI 150)", message["Message"])


class LambdaEventTest(unittest.TestCase):
    """Events shaped the way API Gateway (HTTP API) and EventBridge send them."""

    def http(self, method, path, query=None, body=None):
        return {"version": "2.0", "routeKey": "$default", "rawPath": path, "queryStringParameters": query,
                "requestContext": {"http": {"method": method, "path": path}, "stage": "$default"},
                "body": body, "isBase64Encoded": False}

    def test_serves_the_page_and_the_school_list(self):
        page = lambda_handler(self.http("GET", "/"))
        self.assertEqual(page["statusCode"], 200)
        self.assertIn("BreatheWise", page["body"])
        with fake_boto3(s3=FakeS3()), mock.patch.object(store, "BUCKET", "bucket"):
            schools = lambda_handler(self.http("GET", "/api/schools"))
        self.assertEqual(json.loads(schools["body"])["schools"][0]["id"], "test-school")

    def test_bad_requests_get_clear_errors(self):
        self.assertEqual(lambda_handler(self.http("GET", "/nope"))["statusCode"], 404)
        self.assertEqual(lambda_handler(self.http("POST", "/api/plan", body="not json"))["statusCode"], 400)
        with fake_boto3(s3=FakeS3()), mock.patch.object(store, "BUCKET", "bucket"):
            self.assertEqual(lambda_handler(self.http("GET", "/api/plan", {"school": "missing"}))["statusCode"], 404)

    def test_scheduled_event_runs_the_evening_job(self):
        scheduled = {"version": "0", "source": "aws.events", "detail-type": "Scheduled Event", "detail": {}}
        sns = FakeSNS()
        air = types.SimpleNamespace(
            by_date={}, current_aqi=300, utc_offset=19800, timezone="Asia/Kolkata",
            local_now=lambda: __import__("datetime").datetime(2026, 11, 15, 19, 0),
            hourly=lambda day: {h: (320 if h < 12 else 150) for h in range(24)})
        with fake_boto3(s3=FakeS3(), sns=sns), mock.patch.object(store, "BUCKET", "bucket"), \
                mock.patch("app.handler.fetch_air", return_value=air), \
                mock.patch.dict(os.environ, {"ALERT_TOPIC_ARN": "arn:topic"}):
            result = lambda_handler(scheduled)
        self.assertEqual(result["sent"], [{"school": "test-school", "date": "2026-11-16", "sent_to": "sns"}])
        self.assertIn("Move 1 period to cleaner hours.", sns.sent[0]["Subject"])

    def test_evening_job_stays_quiet_on_days_with_no_changes(self):
        sns = FakeSNS()
        air = types.SimpleNamespace(
            by_date={}, current_aqi=80, utc_offset=19800, timezone="Asia/Kolkata",
            local_now=lambda: __import__("datetime").datetime(2026, 11, 15, 19, 0),
            hourly=lambda day: {h: 80 for h in range(24)})
        with fake_boto3(s3=FakeS3(), sns=sns), mock.patch.object(store, "BUCKET", "bucket"), \
                mock.patch("app.handler.fetch_air", return_value=air), \
                mock.patch.dict(os.environ, {"ALERT_TOPIC_ARN": "arn:topic"}):
            result = lambda_handler({"source": "aws.events"})
        self.assertEqual(result["sent"], [{"school": "test-school", "date": "2026-11-16", "skipped": "no changes needed"}])
        self.assertEqual(sns.sent, [])

    def test_asthma_counts_in_an_upload_are_checked(self):
        for bad in ({"7B": "lots"}, {"7B": -1}, ["7B"]):
            body = json.dumps({"csv": CSV, "lat": 28.6, "lon": 77.2, "asthma": bad})
            self.assertEqual(lambda_handler(self.http("POST", "/api/plan", body=body))["statusCode"], 400)

    def test_oversized_upload_is_refused(self):
        body = json.dumps({"csv": "x" * 250_000, "lat": 28.6, "lon": 77.2})
        reply = lambda_handler(self.http("POST", "/api/plan", body=body))
        self.assertEqual(reply["statusCode"], 400)
        self.assertIn("too large", reply["body"])

    def test_direct_invoke_with_a_date_sends_that_days_plan(self):
        sns = FakeSNS()
        asked = []
        air = types.SimpleNamespace(
            by_date={}, current_aqi=80, utc_offset=19800, timezone="Asia/Kolkata",
            local_now=lambda: __import__("datetime").datetime(2026, 10, 10, 9, 0),
            hourly=lambda day: {h: (320 if h < 12 else 150) for h in range(24)})

        def fake_fetch(lat, lon, day, tz, days=1):
            asked.append(day)
            return air

        with fake_boto3(s3=FakeS3(), sns=sns), mock.patch.object(store, "BUCKET", "bucket"), \
                mock.patch("app.handler.fetch_air", side_effect=fake_fetch), \
                mock.patch.dict(os.environ, {"ALERT_TOPIC_ARN": "arn:topic"}):
            result = lambda_handler({"date": "2025-11-17"})
            with mock.patch("builtins.print"):
                bad = lambda_handler({"date": "next tuesday"})
        # First the forecast window is fetched; the date is outside it, so that day is fetched too.
        self.assertEqual(asked[:2], [None, date(2025, 11, 17)])
        self.assertEqual(result["sent"][0]["date"], "2025-11-17")
        self.assertIn("Air plan for 2025-11-17", sns.sent[0]["Subject"])
        self.assertIn("Date must look like", bad["sent"][0]["error"])
        self.assertEqual(len(sns.sent), 1)



def fake_air(now, morning, afternoon, current=80):
    return types.SimpleNamespace(
        by_date={}, current_aqi=current, utc_offset=19800, timezone="Asia/Kolkata",
        local_now=lambda: now, hourly=lambda day: {h: (morning if h < 12 else afternoon) for h in range(24)})


class MorningUpdateTest(unittest.TestCase):
    """Evening plan is saved; the morning run reports only what has changed."""

    def run_job(self, s3, sns, event, air):
        with fake_boto3(s3=s3, sns=sns), mock.patch.object(store, "BUCKET", "bucket"), \
                mock.patch("app.handler.fetch_air", return_value=air), \
                mock.patch.dict(os.environ, {"ALERT_TOPIC_ARN": "arn:topic"}):
            return lambda_handler(event)

    def test_morning_reports_only_the_difference(self):
        import datetime
        s3, sns = FakeS3(), FakeSNS()
        evening = datetime.datetime(2026, 11, 15, 19, 0)      # Sunday evening, planning Monday
        self.run_job(s3, sns, {"source": "aws.events"}, fake_air(evening, 320, 150))
        saved = json.loads(s3.objects["plans/test-school/2026-11-16.json"])
        self.assertEqual([(a["activity"], a["action"], a["to"]) for a in saved["actions"]], [("PT", "swap", "13:00")])
        self.assertEqual(len(sns.sent), 1)

        # By morning the afternoon is forecast to be bad too, so PT can no longer move.
        morning = datetime.datetime(2026, 11, 16, 6, 30)
        result = self.run_job(s3, sns, {"run": "morning"}, fake_air(morning, 320, 300, current=300))
        self.assertEqual(result["sent"], [{"school": "test-school", "date": "2026-11-16", "changes": 1, "sent_to": "sns"}])
        update = sns.sent[1]
        self.assertIn("1 decision has changed since last night", update["Subject"])
        self.assertIn('PT (08:00-08:40) was "moves to 13:00", now "held indoors"', update["Message"])

    def test_morning_stays_quiet_when_nothing_changed_or_there_is_nothing_to_compare(self):
        import datetime
        s3, sns = FakeS3(), FakeSNS()
        morning = datetime.datetime(2026, 11, 16, 6, 30)
        first = self.run_job(s3, sns, {"run": "morning"}, fake_air(morning, 320, 150))
        self.assertEqual(first["sent"][0]["skipped"], "no plan from last night to compare")
        self.run_job(s3, sns, {"source": "aws.events"}, fake_air(datetime.datetime(2026, 11, 15, 19, 0), 320, 150))
        again = self.run_job(s3, sns, {"run": "morning"}, fake_air(morning, 320, 150, current=320))
        self.assertEqual(again["sent"][0]["skipped"], "nothing has changed since last night")
        self.assertEqual(len(sns.sent), 1)   # only the evening plan went out

    def test_page_shows_what_changed_since_the_saved_plan(self):
        import datetime
        s3, sns = FakeS3(), FakeSNS()
        self.run_job(s3, sns, {"source": "aws.events"}, fake_air(datetime.datetime(2026, 11, 15, 19, 0), 320, 150))
        event = {"rawPath": "/api/plan", "queryStringParameters": {"school": "test-school", "date": "2026-11-16"},
                 "requestContext": {"http": {"method": "GET"}}}
        air = fake_air(datetime.datetime(2026, 11, 16, 6, 30), 320, 300, current=300)
        air.by_date = {"2026-11-16": air.hourly(None)}
        plan = json.loads(self.run_job(s3, sns, event, air)["body"])
        self.assertEqual([(c["activity"], c["was"], c["now"]) for c in plan["since_last_plan"]["changes"]],
                         [("PT", "moves to 13:00", "held indoors")])


class SettingsTest(unittest.TestCase):
    """A stored school's settings can be changed from the web only with the deploy-time passcode."""

    PASS = "unit-test-passcode"

    def call(self, s3, path, body, passcode_set=True):
        event = {"rawPath": path, "requestContext": {"http": {"method": "POST"}}, "body": json.dumps(body)}
        env = {"ADMIN_PASSCODE": self.PASS} if passcode_set else {"ADMIN_PASSCODE": ""}
        with fake_boto3(s3=s3), mock.patch.object(store, "BUCKET", "bucket"), mock.patch.dict(os.environ, env), \
                mock.patch("app.handler.time.sleep"):
            reply = lambda_handler(event)
        return reply["statusCode"], json.loads(reply["body"])

    def test_wrong_or_missing_passcode_is_refused(self):
        s3 = FakeS3()
        for body in ({"school": "test-school"}, {"school": "test-school", "passcode": "guess"}, {"school": "test-school", "passcode": 12345}):
            self.assertEqual(self.call(s3, "/api/settings/read", body)[0], 403)
            self.assertEqual(self.call(s3, "/api/settings/save", {**body, "settings": {"name": "Hacked"}})[0], 403)
        self.assertEqual(json.loads(s3.objects["schools/test-school/school.json"])["name"], "Test School")

    def test_editing_is_off_when_no_passcode_was_set_at_deploy(self):
        status, body = self.call(FakeS3(), "/api/settings/read", {"school": "test-school", "passcode": ""}, passcode_set=False)
        self.assertEqual(status, 403)
        self.assertIn("not switched on", body["error"])

    def test_read_and_save_with_the_passcode(self):
        s3 = FakeS3()
        status, body = self.call(s3, "/api/settings/read", {"school": "test-school", "passcode": self.PASS})
        self.assertEqual((status, body["settings"]["thresholds"]["primary_limit"], body["periods"]), (200, 200, 2))
        new = {"thresholds": {"senior_limit": 180, "avoid_last_period": 1}, "asthma": {"7b": 3}, "students": {"school": 400, "per_class": 40}}
        status, body = self.call(s3, "/api/settings/save", {"school": "test-school", "passcode": self.PASS, "settings": new})
        self.assertEqual(status, 200)
        stored = json.loads(s3.objects["schools/test-school/school.json"])
        self.assertEqual((stored["thresholds"]["senior_limit"], stored["asthma"], stored["students"]["school"], stored["lat"]),
                         (180, {"7B": 3}, 400, 28.6))

    def test_bad_values_and_unreadable_timetables_are_refused(self):
        s3 = FakeS3()
        base = {"school": "test-school", "passcode": self.PASS}
        self.assertEqual(self.call(s3, "/api/settings/save", {**base, "settings": {"thresholds": {"senior_limit": 9999}}})[0], 400)
        self.assertEqual(self.call(s3, "/api/settings/save", {**base, "settings": {}, "csv": "not,a,timetable\n1,2,3\n"})[0], 400)
        self.assertEqual(self.call(s3, "/api/settings/save", {**base, "school": "missing", "settings": {}})[0], 404)
        self.assertEqual(s3.objects["schools/test-school/timetable.csv"], CSV)
        good = CSV + "mon,7B,09:00,09:40,Games,yes\n"
        self.assertEqual(self.call(s3, "/api/settings/save", {**base, "settings": {}, "csv": good})[0], 200)
        self.assertEqual(s3.objects["schools/test-school/timetable.csv"], good)


if __name__ == "__main__":
    unittest.main()
