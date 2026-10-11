"""Write the parent notice for a plan, in English and Hindi.

With USE_BEDROCK=1 the wording comes from Amazon Bedrock; otherwise, or if the
call fails, a fixed template is used. Either way the facts come from the plan.
"""

import json
import os
from datetime import date

HI_DAYS = ["सोमवार", "मंगलवार", "बुधवार", "गुरुवार", "शुक्रवार", "शनिवार", "रविवार"]
HI_MONTHS = ["जनवरी", "फ़रवरी", "मार्च", "अप्रैल", "मई", "जून", "जुलाई", "अगस्त", "सितंबर", "अक्टूबर", "नवंबर", "दिसंबर"]

DEFAULT_MODEL_ID = "eu.amazon.nova-lite-v1:0"
_cache = {}  # facts -> notice, so page refreshes do not pay for the same notice twice

SYSTEM_PROMPT = """You write short notices from a school to parents about air quality.
You are given a JSON plan produced by the school's scheduling system.
Rules:
- Use only the facts in the plan. Never invent or change numbers, times, classes or activities.
- Say what the school is doing, and ask parents to tell the class teacher if their child has asthma or another breathing condition.
- Do not recommend masks. Do not say indoor air is clean or safe; the point is less heavy outdoor activity in the worst air.
- Plain, calm language. No alarm, no medical claims. Under 110 words per language.
- Reply with JSON only: {"en": "<English notice>", "hi": "<Hindi notice in Devanagari>"}"""


def write_notice(school_name, plan):
    """Return {"en": ..., "hi": ..., "source": "bedrock"|"template"}."""
    # A clear day needs one fixed sentence, so the model is only used when there are changes.
    if os.environ.get("USE_BEDROCK") == "1" and plan["changes"]:
        try:
            return {**_bedrock_notice(school_name, plan), "source": "bedrock"}
        except Exception as exc:  # fall back so the plan is never blocked on the model
            print(f"Bedrock notice failed, using template: {exc}")
    return {**template_notice(school_name, plan), "source": "template"}


def _bedrock_notice(school_name, plan):
    import boto3

    day = date.fromisoformat(plan["date"])
    facts = json.dumps({
        "school": school_name,
        "date_english": f"{day.strftime('%A')}, {day.day} {day.strftime('%B')}",
        "date_hindi": f"{HI_DAYS[day.weekday()]}, {day.day} {HI_MONTHS[day.month - 1]}",
        "peak_aqi": plan["peak_aqi"],
        "changes": [a for a in plan["actions"] if a["action"] in ("swap", "indoors")],
    }, ensure_ascii=False, sort_keys=True)
    if facts in _cache:
        return _cache[facts]
    # Bedrock can live in a different region from the rest of the app (BEDROCK_REGION).
    client = boto3.client("bedrock-runtime", region_name=os.environ.get("BEDROCK_REGION") or None)
    response = client.converse(
        modelId=os.environ.get("BEDROCK_MODEL_ID", DEFAULT_MODEL_ID),
        system=[{"text": SYSTEM_PROMPT}],
        messages=[{"role": "user", "content": [{"text": facts}]}],
        inferenceConfig={"maxTokens": 900, "temperature": 0.2},
    )
    text = response["output"]["message"]["content"][0]["text"].strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    notice = json.loads(text)
    if not (isinstance(notice.get("en"), str) and isinstance(notice.get("hi"), str)):
        raise ValueError("model reply did not contain both languages")
    if len(_cache) > 200:
        _cache.clear()
    _cache[facts] = {"en": notice["en"].strip(), "hi": notice["hi"].strip()}
    return _cache[facts]


def _change_lines(plan, lang):
    lines = []
    for a in plan["actions"]:
        slot = f"{a['start']}–{a['end']}"
        if a["action"] == "swap":
            s = a["swap_with"]
            if lang == "en":
                lines.append(f"• Class {a['class']}: {a['activity']} moves from {slot} to {s['start']}–{s['end']}.")
            else:
                lines.append(f"• कक्षा {a['class']}: {a['activity']} अब {slot} की जगह {s['start']}–{s['end']} पर होगा।")
        elif a["action"] == "indoors":
            who = "Whole school" if a["class"] == "ALL" else f"Class {a['class']}"
            who_hi = "पूरा विद्यालय" if a["class"] == "ALL" else f"कक्षा {a['class']}"
            if lang == "en":
                lines.append(f"• {who}: {a['activity']} ({slot}) will be held indoors as a light activity.")
            else:
                lines.append(f"• {who_hi}: {a['activity']} ({slot}) भवन के अंदर हल्की गतिविधि के रूप में होगा।")
    return "\n".join(lines)


def template_notice(school_name, plan):
    day = date.fromisoformat(plan["date"])
    date_en = f"{day.strftime('%A')}, {day.day} {day.strftime('%B')}"
    date_hi = f"{HI_DAYS[day.weekday()]}, {day.day} {HI_MONTHS[day.month - 1]}"
    if plan["verdict"] in ("clear", "no_school"):
        return {
            "en": f"Dear parents, the air quality forecast for {date_en} is within {school_name}'s limits. "
                  "All outdoor activities will run as scheduled.",
            "hi": f"प्रिय अभिभावक, {date_hi} के लिए वायु गुणवत्ता का पूर्वानुमान {school_name} की तय सीमा के भीतर है। "
                  "सभी बाहरी गतिविधियाँ तय समय पर होंगी।",
        }
    return {
        "en": f"Dear parents,\n\nAir quality on {date_en} is forecast to be poor during part of the school day "
              f"(AQI up to about {plan['peak_aqi']}). So that children are not running or exercising outdoors in the worst air, "
              f"{school_name} is making these changes:\n\n{_change_lines(plan, 'en')}\n\nOutdoor activity that goes "
              "ahead will be kept light for children with asthma or other breathing conditions. Please tell the class "
              "teacher if this applies to your child.",
        "hi": f"प्रिय अभिभावक,\n\n{date_hi} को विद्यालय के समय के कुछ हिस्से में वायु गुणवत्ता खराब रहने का अनुमान है "
              f"(AQI लगभग {plan['peak_aqi']} तक)। सबसे खराब हवा में बच्चे बाहर दौड़-भाग या व्यायाम न करें, इसलिए {school_name} ये बदलाव कर रहा है:"
              f"\n\n{_change_lines(plan, 'hi')}\n\nजो बाहरी गतिविधियाँ होंगी, उनमें दमा या साँस की तकलीफ़ वाले बच्चों से "
              "केवल हल्की गतिविधि कराई जाएगी। यदि आपके बच्चे को ऐसी कोई तकलीफ़ है तो कृपया कक्षा अध्यापक को बताएँ।",
    }
