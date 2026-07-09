"""
biotracking uv_fetcher.py
-------------------------
Fetches UV index data from the Open-Meteo API.

Open-Meteo is free, requires no API key, and receives only
coordinates and a date - no personal health data ever leaves
this machine.

API docs: https://open-meteo.com/en/docs

Usage:
    from uv_fetcher import fetch_uv_for_date, fetch_uv_range

    uv = fetch_uv_for_date("2025-01-03")
    # returns: {"date": "2025-01-03", "uv_morning": 1.2,
    #           "uv_noon": 4.8, "uv_evening": 0.3, "source": "api"}
"""

import json
import os
from datetime import date, datetime, timedelta
from typing import Optional

import requests


# ============================================================
# Config loading
# ============================================================

def load_config() -> dict:
    """Load location config from config.json (from SARDINE_DATA_DIR when set —
    see DATA_DIR in app.py)."""
    config_dir = os.environ.get("SARDINE_DATA_DIR", os.path.dirname(__file__))
    config_path = os.path.join(config_dir, "config.json")
    if not os.path.exists(config_path):
        raise FileNotFoundError(
            "config.json not found. Run setup.py first."
        )
    with open(config_path) as f:
        return json.load(f)


# ============================================================
# Open-Meteo API
# ============================================================

BASE_URL = "https://api.open-meteo.com/v1/forecast"

# Hours to sample for morning / noon / evening UV
# Using local solar time approximations:
#   Morning:  9am  (index 9)
#   Noon:     12pm (index 12)
#   Evening:  5pm  (index 17)
HOUR_MORNING = 9
HOUR_NOON = 12
HOUR_EVENING = 17

# Open-Meteo forecast API notes:
# - Querying a single date for "today" returns unresolved forecast hours (all zeros)
# - Querying a range that includes yesterday forces resolved model data
# - Maximum historical range on the forecast endpoint is ~16 days back
# - For dates older than 16 days, use fetch_uv_range() with explicit start/end
#   that anchor to a known-good window, or use a separate historical source
# - The archive endpoint does NOT support uv_index (returns null for all hours)


def _build_params(lat: float, lon: float,
                  start_date: str, end_date: str,
                  timezone: str) -> dict:
    """Build the Open-Meteo request parameters.
    
    Always requests at least a two-day window ending on end_date,
    because single-day requests for today return unresolved zeros.
    The caller should filter results to the date they actually want.
    """
    # Ensure we always request at least two days
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    if start == end:
        start = start - timedelta(days=1)

    return {
        "latitude": lat,
        "longitude": lon,
        "hourly": "uv_index",
        "timezone": timezone,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    }


def _extract_uv_for_date(data: dict, target_date: str) -> Optional[dict]:
    """Extract morning/noon/evening UV values for a single date
    from the Open-Meteo hourly response.

    Args:
        data: parsed JSON response from Open-Meteo
        target_date: YYYY-MM-DD string

    Returns:
        dict with uv_morning, uv_noon, uv_evening or None if date not found
    """
    times = data.get("hourly", {}).get("time", [])
    uv_values = data.get("hourly", {}).get("uv_index", [])

    if not times or not uv_values:
        return None

    # Build a lookup: "YYYY-MM-DDTHH:00" -> uv_value
    uv_by_time = dict(zip(times, uv_values))

    def get_hour(hour: int) -> Optional[float]:
        key = f"{target_date}T{hour:02d}:00"
        val = uv_by_time.get(key)
        # Round to 2 decimal places, treat None as 0.0
        return round(float(val), 2) if val is not None else 0.0

    return {
        "date": target_date,
        "uv_morning": get_hour(HOUR_MORNING),
        "uv_noon": get_hour(HOUR_NOON),
        "uv_evening": get_hour(HOUR_EVENING),
        "source": "api",
    }


def fetch_uv_for_date(target_date: str) -> Optional[dict]:
    """Fetch UV index for a single date.

    Args:
        target_date: YYYY-MM-DD string

    Returns:
        dict with date, uv_morning, uv_noon, uv_evening, source
        or None if the request fails
    """
    try:
        config = load_config()
        params = _build_params(
            lat=config["location_lat"],
            lon=config["location_lon"],
            start_date=target_date,
            end_date=target_date,
            timezone=config.get("timezone", "America/Chicago"),
        )

        response = requests.get(BASE_URL, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        return _extract_uv_for_date(data, target_date)

    except requests.exceptions.ConnectionError:
        # No internet - return None, caller decides whether to use manual entry
        return None
    except requests.exceptions.Timeout:
        return None
    except requests.exceptions.HTTPError as e:
        print(f"UV API HTTP error for {target_date}: {e}")
        return None
    except Exception as e:
        print(f"UV fetch error for {target_date}: {e}")
        return None


def fetch_uv_range(start_date: str, end_date: str) -> list[dict]:
    """Fetch UV index for a range of dates in a single API call.
    More efficient than calling fetch_uv_for_date in a loop.

    Args:
        start_date: YYYY-MM-DD
        end_date: YYYY-MM-DD

    Returns:
        List of dicts, one per date in the range.
        Dates with no data are skipped rather than returned as None.
    """
    try:
        config = load_config()
        params = _build_params(
            lat=config["location_lat"],
            lon=config["location_lon"],
            start_date=start_date,
            end_date=end_date,
            timezone=config.get("timezone", "America/Chicago"),
        )

        response = requests.get(BASE_URL, params=params, timeout=15)
        response.raise_for_status()
        data = response.json()

        # Walk through each date in the range
        results = []
        current = datetime.strptime(start_date, "%Y-%m-%d").date()
        end = datetime.strptime(end_date, "%Y-%m-%d").date()

        while current <= end:
            date_str = current.isoformat()
            uv = _extract_uv_for_date(data, date_str)
            if uv:
                results.append(uv)
            current += timedelta(days=1)

        return results

    except requests.exceptions.ConnectionError:
        return []
    except Exception as e:
        print(f"UV range fetch error ({start_date} to {end_date}): {e}")
        return []


def fetch_and_store_uv_for_date(target_date: str,
                                location_key: Optional[str] = None) -> Optional[dict]:
    """Fetch UV for a date and store it in the database immediately.
    Returns the UV dict on success, None on failure.

    This is the function Flask routes should call - it handles
    both the API call and the database write in one step.
    """
    import db

    config = load_config()
    if location_key is None:
        location_key = db.make_location_key(
            config["location_lat"], config["location_lon"]
        )

    # Check if we already have it stored
    existing = db.get_uv_data(location_key, target_date)
    if existing and existing.get("source") in ("api", "manual"):
        return existing

    # Fetch from API
    uv = fetch_uv_for_date(target_date)
    if uv:
        db.upsert_uv_data(
            location_key=location_key,
            date_str=uv["date"],
            uv_morning=uv["uv_morning"],
            uv_noon=uv["uv_noon"],
            uv_evening=uv["uv_evening"],
            source="api",
        )
        return uv

    return None


def fetch_and_store_uv_range(start_date: str, end_date: str,
                             location_key: Optional[str] = None) -> int:
    """Fetch and store UV data for a date range.
    Skips dates already stored from the API.
    Returns count of dates successfully stored.
    """
    import db

    config = load_config()
    if location_key is None:
        location_key = db.make_location_key(
            config["location_lat"], config["location_lon"]
        )

    results = fetch_uv_range(start_date, end_date)
    stored = 0

    for uv in results:
        existing = db.get_uv_data(location_key, uv["date"])
        if existing and existing.get("source") in ("api", "manual"):
            continue
        db.upsert_uv_data(
            location_key=location_key,
            date_str=uv["date"],
            uv_morning=uv["uv_morning"],
            uv_noon=uv["uv_noon"],
            uv_evening=uv["uv_evening"],
            source="api",
        )
        stored += 1

    return stored


def backfill_uv_from_tracker_start(user_id: int = 1) -> int:
    """Backfill UV data for all dates in daily_observations
    that don't yet have UV data stored.

    Returns count of dates successfully backfilled.
    """
    import db

    config = load_config()
    location_key = db.make_location_key(
        config["location_lat"], config["location_lon"]
    )

    observations = db.get_all_daily_observations(user_id)
    if not observations:
        print("No daily observations found to backfill.")
        return 0

    dates_needing_uv = []
    for obs in observations:
        existing = db.get_uv_data(location_key, obs["date"])
        if not existing:
            dates_needing_uv.append(obs["date"])

    if not dates_needing_uv:
        print("UV data already present for all observation dates.")
        return 0

    start = min(dates_needing_uv)
    end = max(dates_needing_uv)

    print(f"Backfilling UV data for {len(dates_needing_uv)} dates "
          f"({start} to {end})...")

    stored = fetch_and_store_uv_range(start, end, location_key)
    print(f"Backfill complete. Stored UV data for {stored} dates.")
    return stored


# ============================================================
# Manual UV entry fallback
# ============================================================

def store_manual_uv(date_str: str, uv_morning: float,
                    uv_noon: float, uv_evening: float,
                    location_key: Optional[str] = None) -> bool:
    """Store manually entered UV values when API is unavailable.
    Marks source as 'manual' so it's distinguishable in analysis.
    """
    import db

    if location_key is None:
        config = load_config()
        location_key = db.make_location_key(
            config["location_lat"], config["location_lon"]
        )

    db.upsert_uv_data(
        location_key=location_key,
        date_str=date_str,
        uv_morning=uv_morning,
        uv_noon=uv_noon,
        uv_evening=uv_evening,
        source="manual",
    )
    return True


# ============================================================
# Weather forecast (cloud cover + high temp for today)
# ============================================================

WEATHER_URL = "https://api.open-meteo.com/v1/forecast"


def fetch_weather_for_today(lat: float, lon: float,
                            timezone: str = "America/Chicago") -> Optional[dict]:
    """Fetch today's cloud cover and high temperature from Open-Meteo.
    Returns {cloud_cover_pct, temperature_high, weather_summary} or None.
    """
    try:
        today_str = date.today().isoformat()
        response = requests.get(WEATHER_URL, params={
            "latitude": lat,
            "longitude": lon,
            "hourly": "cloud_cover",
            "daily": "temperature_2m_max",
            "temperature_unit": "fahrenheit",
            "timezone": timezone,
            "start_date": today_str,
            "end_date": today_str,
        }, timeout=10)
        response.raise_for_status()
        data = response.json()

        # Daytime average cloud cover (8am-6pm = hours 8-17)
        hourly_cc = data.get("hourly", {}).get("cloud_cover", [])
        daytime = hourly_cc[8:18] if len(hourly_cc) >= 18 else hourly_cc
        avg_cc = sum(daytime) / len(daytime) if daytime else None

        # High temp
        daily_max = data.get("daily", {}).get("temperature_2m_max", [])
        high_temp = daily_max[0] if daily_max else None

        # Derive summary
        if avg_cc is not None:
            if avg_cc < 25:
                summary = "sunny"
            elif avg_cc < 50:
                summary = "partly cloudy"
            elif avg_cc < 75:
                summary = "mostly cloudy"
            else:
                summary = "overcast"
        else:
            summary = None

        return {
            "cloud_cover_pct": round(avg_cc, 1) if avg_cc is not None else None,
            "temperature_high": round(high_temp, 1) if high_temp is not None else None,
            "weather_summary": summary,
        }
    except Exception as e:
        print(f"[weather] fetch failed: {e}")
        return None


# ============================================================
# Smart UV fetch — handles today's zeros gracefully
# ============================================================

def smart_fetch_uv_for_date(target_date: str,
                            location_key: Optional[str] = None,
                            timezone: str = "America/Chicago") -> Optional[dict]:
    """Smart UV fetch that handles today's unresolved data.

    - Past dates: normal API fetch + store
    - Today before 8pm local: return yesterday's UV as fallback + weather forecast
    - Today after 8pm local: fetch today's resolved UV normally
    - Future dates: return None
    """
    import db

    config = load_config()
    if location_key is None:
        location_key = db.make_location_key(
            config["location_lat"], config["location_lon"]
        )

    target = datetime.strptime(target_date, "%Y-%m-%d").date()
    today = date.today()

    # Future dates — nothing to fetch
    if target > today:
        return None

    # Past dates — use existing logic
    if target < today:
        return fetch_and_store_uv_for_date(target_date, location_key)

    # Today — check local time
    try:
        from zoneinfo import ZoneInfo
        local_now = datetime.now(ZoneInfo(timezone))
    except Exception:
        local_now = datetime.now()

    if local_now.hour >= 20:
        # After 8pm — today's data should be resolved
        return fetch_and_store_uv_for_date(target_date, location_key)

    # Before 8pm — today's UV is unreliable. Use yesterday + forecast.
    # First check if we already have good data stored for today (manual entry)
    existing_today = db.get_uv_data(location_key, target_date)
    if existing_today and existing_today.get("source") in ("manual", "api"):
        # Manual entry or a late-day re-fetch already stored
        if existing_today.get("uv_noon") and existing_today["uv_noon"] > 0:
            return existing_today

    # Fall back to yesterday's UV
    yesterday_str = (today - timedelta(days=1)).isoformat()
    yesterday_uv = db.get_uv_data(location_key, yesterday_str)
    if not yesterday_uv:
        # Try to fetch yesterday if not stored
        yesterday_uv = fetch_and_store_uv_for_date(yesterday_str, location_key)

    # Fetch today's weather forecast
    lat = config.get("location_lat")
    lon = config.get("location_lon")
    forecast = None
    if lat and lon:
        forecast = fetch_weather_for_today(lat, lon, timezone)

    if yesterday_uv:
        return {
            "uv_morning": yesterday_uv.get("uv_morning", 0),
            "uv_noon": yesterday_uv.get("uv_noon", 0),
            "uv_evening": yesterday_uv.get("uv_evening", 0),
            "source": "yesterday",
            "forecast": forecast,
        }

    # No yesterday data either — return forecast-only stub
    if forecast:
        return {
            "uv_morning": 0,
            "uv_noon": 0,
            "uv_evening": 0,
            "source": "unavailable",
            "forecast": forecast,
        }

    return None


# ============================================================
# Quick test - run this file directly to verify API connectivity
# ============================================================

if __name__ == "__main__":
    today = date.today()
    yesterday = (today - timedelta(days=1)).isoformat()
    today_str = today.isoformat()

    results = fetch_uv_range(yesterday, today_str)
    for r in results:
        print(f"{r['date']}  "
              f"morning: {r['uv_morning']}  "
              f"noon: {r['uv_noon']}  "
              f"evening: {r['uv_evening']}")