"""Hourly air quality for one place, from the Open-Meteo API.

Open-Meteo serves forecasts, the current hour and past days from the same
endpoint, so one call backs today's live view, tomorrow's plan and a replay of
a past bad-air day. The values are modelled, not readings from a sensor at the
school.
"""

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .aqi import indian_aqi

API_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
FORECAST_DAYS = 5


class ForecastError(Exception):
    pass


@dataclass
class AirData:
    by_date: dict        # {"2026-11-18": {hour: aqi}}
    current_aqi: int     # None if the service sent no current value
    utc_offset: int      # seconds east of UTC at the school
    timezone: str

    def local_now(self):
        return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=self.utc_offset)

    def hourly(self, day):
        hours = self.by_date.get(day.isoformat())
        if not hours:
            raise ForecastError(f"No air quality data is available for {day.isoformat()}.")
        return hours


def fetch_air(lat, lon, day=None, tz="auto", days=1):
    """Air data for `days` days from `day`, or for the next few days when `day` is None."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "pm10,pm2_5",
        "current": "pm10,pm2_5",
        "timezone": tz,
    }
    if day is None:
        params["forecast_days"] = FORECAST_DAYS
    else:
        params["start_date"] = day.isoformat()
        params["end_date"] = (day + timedelta(days=days - 1)).isoformat()
    try:
        with urllib.request.urlopen(f"{API_URL}?{urllib.parse.urlencode(params)}", timeout=15) as resp:
            payload = json.load(resp)
    except Exception as exc:
        raise ForecastError(f"Could not reach the air quality service: {exc}") from exc
    return parse_air(payload)


def parse_air(payload):
    hourly = payload.get("hourly") or {}
    times = hourly.get("time") or []
    pm25 = hourly.get("pm2_5") or []
    pm10 = hourly.get("pm10") or []
    by_date = {}
    for i, stamp in enumerate(times):
        aqi = indian_aqi(pm25[i] if i < len(pm25) else None, pm10[i] if i < len(pm10) else None)
        if aqi is not None:
            by_date.setdefault(stamp[:10], {})[int(stamp[11:13])] = aqi
    if not by_date:
        raise ForecastError("No air quality data is available for that place and date.")
    current = payload.get("current") or {}
    return AirData(
        by_date=by_date,
        current_aqi=indian_aqi(current.get("pm2_5"), current.get("pm10")),
        utc_offset=int(payload.get("utc_offset_seconds") or 0),
        timezone=payload.get("timezone") or "UTC",
    )
