"""Send the evening plan to school staff: SNS on AWS, the outbox/ folder locally."""

import os
from pathlib import Path

OUTBOX = Path(__file__).resolve().parent.parent / "outbox"


def staff_message(school_name, plan, notice):
    lines = [f"{school_name}: air plan for {plan['weekday']}, {plan['date']}", "", plan["headline"]]
    if plan["peak_aqi"] is not None:
        lines.append(f"Peak AQI during school hours: about {plan['peak_aqi']}.")
    changes = [a for a in plan["actions"] if a["action"] in ("swap", "indoors")]
    if plan.get("impact"):
        lines.append(f"This plan avoids {plan['impact']['minutes_over_limit_avoided']} minutes of outdoor "
                     f"activity in air over your limits, and keeps {plan['impact']['outdoor_minutes_kept']} minutes "
                     "of outdoor activity going in cleaner hours.")
    if changes:
        lines.append("")
    for a in changes:
        who = "Whole school" if a["class"] == "ALL" else f"Class {a['class']}"
        slot = f"{a['start']}-{a['end']}"
        if a["action"] == "swap":
            s = a["swap_with"]
            lines.append(f"- {who}: swap {a['activity']} ({slot}, AQI {a['aqi']}) with "
                         f"{s['activity']} ({s['start']}-{s['end']}, AQI {s['aqi']}).")
        else:
            lines.append(f"- {who}: hold {a['activity']} ({slot}, AQI {a['aqi']}) indoors as a light activity.")
        if a.get("also_on"):
            lines[-1] += f" Also needed on {a['also_on']} of the next {plan.get('other_days', a['also_on'])} school days."
    lines += ["", "Notice for parents (English):", notice["en"], "", "Notice for parents (Hindi):", notice["hi"]]
    return "\n".join(lines)


def send(school_id, school_name, plan, notice):
    subject = f"Air plan for {plan['date']}: {plan['headline']}"[:100]
    body = staff_message(school_name, plan, notice)
    topic = os.environ.get("ALERT_TOPIC_ARN")
    if topic:
        import boto3
        boto3.client("sns").publish(TopicArn=topic, Subject=subject, Message=body)
        return "sns"
    OUTBOX.mkdir(exist_ok=True)
    path = OUTBOX / f"{school_id}-{plan['date']}.txt"
    path.write_text(f"Subject: {subject}\n\n{body}\n", encoding="utf-8")
    return str(path)


def send_update(school_id, school_name, plan, changes):
    """Tell staff only what differs from the plan they were sent the evening before."""
    count = len(changes)
    subject = f"Update for {plan['date']}: {count} {'decision has' if count == 1 else 'decisions have'} changed since last night"[:100]
    lines = [f"{school_name}: the air forecast for {plan['weekday']}, {plan['date']} has moved since last night's plan.", ""]
    for c in changes:
        who = "Whole school" if c["class"] == "ALL" else f"Class {c['class']}"
        lines.append(f"- {who}: {c['activity']} ({c['start']}-{c['end']}) was \"{c['was']}\", now \"{c['now']}\". "
                     f"AQI {c['aqi_was']} last night, {c['aqi_now']} now.")
    lines += ["", "Everything else in last night's plan stands."]
    body = "\n".join(lines)
    topic = os.environ.get("ALERT_TOPIC_ARN")
    if topic:
        import boto3
        boto3.client("sns").publish(TopicArn=topic, Subject=subject, Message=body)
        return "sns"
    OUTBOX.mkdir(exist_ok=True)
    path = OUTBOX / f"{school_id}-{plan['date']}-update.txt"
    path.write_text(f"Subject: {subject}\n\n{body}\n", encoding="utf-8")
    return str(path)
