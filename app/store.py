"""Where schools live: an S3 bucket on AWS, the data/ folder locally.

Each school is a folder schools/<id>/ holding school.json and timetable.csv.
"""

import json
import os
import re
from pathlib import Path

LOCAL_ROOT = Path(__file__).resolve().parent.parent / "data" / "schools"
BUCKET = os.environ.get("SCHOOLS_BUCKET")
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class SchoolNotFound(Exception):
    pass


def list_schools():
    """[{id, name}] for every stored school."""
    if BUCKET:
        import boto3
        s3 = boto3.client("s3")
        pages = s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix="schools/", Delimiter="/")
        ids = [p["Prefix"].split("/")[1] for page in pages for p in page.get("CommonPrefixes", [])]
    else:
        ids = sorted(d.name for d in LOCAL_ROOT.iterdir() if d.is_dir()) if LOCAL_ROOT.exists() else []
    schools = []
    for school_id in ids:
        try:
            meta, _ = get_school(school_id)
        except SchoolNotFound:
            continue
        schools.append({"id": school_id, "name": meta.get("name", school_id)})
    return schools


def get_school(school_id):
    """Return (metadata dict, timetable CSV text)."""
    if not ID_PATTERN.match(school_id or ""):
        raise SchoolNotFound(school_id)
    try:
        if BUCKET:
            import boto3
            s3 = boto3.client("s3")
            read = lambda name: s3.get_object(Bucket=BUCKET, Key=f"schools/{school_id}/{name}")["Body"].read().decode("utf-8")
        else:
            read = lambda name: (LOCAL_ROOT / school_id / name).read_text(encoding="utf-8")
        return json.loads(read("school.json")), read("timetable.csv")
    except SchoolNotFound:
        raise
    except Exception as exc:
        raise SchoolNotFound(school_id) from exc


PLANS_LOCAL = Path(__file__).resolve().parent.parent / "outbox" / "plans"


def save_plan(school_id, day, snapshot):
    """Keep the decisions made for a day, so a later run can say what changed."""
    body = json.dumps(snapshot, ensure_ascii=False)
    if BUCKET:
        import boto3
        boto3.client("s3").put_object(Bucket=BUCKET, Key=f"plans/{school_id}/{day}.json",
                                      Body=body.encode("utf-8"), ContentType="application/json")
    else:
        PLANS_LOCAL.mkdir(parents=True, exist_ok=True)
        (PLANS_LOCAL / f"{school_id}-{day}.json").write_text(body, encoding="utf-8")


def load_plan(school_id, day):
    """The saved decisions for a day, or None if there are none."""
    try:
        if BUCKET:
            import boto3
            raw = boto3.client("s3").get_object(Bucket=BUCKET, Key=f"plans/{school_id}/{day}.json")["Body"].read()
            return json.loads(raw.decode("utf-8"))
        return json.loads((PLANS_LOCAL / f"{school_id}-{day}.json").read_text(encoding="utf-8"))
    except Exception:
        return None


def save_school(school_id, meta, csv_text=None):
    """Replace an existing school's settings and, if given, its timetable."""
    get_school(school_id)   # raises SchoolNotFound: schools are created by the deployer, not from the web
    files = {"school.json": json.dumps(meta, indent=2, ensure_ascii=False) + "\n"}
    if csv_text is not None:
        files["timetable.csv"] = csv_text
    for name, body in files.items():
        if BUCKET:
            import boto3
            boto3.client("s3").put_object(Bucket=BUCKET, Key=f"schools/{school_id}/{name}", Body=body.encode("utf-8"))
        else:
            (LOCAL_ROOT / school_id / name).write_text(body, encoding="utf-8")
