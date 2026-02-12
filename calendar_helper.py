import os
import json
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/calendar"]

# ----------------------------
# Google Calendar service
# ----------------------------

def get_service():
    raw = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if not raw:
        raise RuntimeError("GOOGLE_CREDENTIALS_JSON not set in Render env")

    try:
        creds_info = json.loads(raw)
    except Exception as e:
        raise RuntimeError("GOOGLE_CREDENTIALS_JSON is not valid JSON: " + str(e))

    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


# ----------------------------
# Helpers
# ----------------------------

def overlaps(a_start, a_end, b_start, b_end):
    # True if time ranges overlap at all
    return a_start < b_end and a_end > b_start


def ceil_to_step(dt: datetime, step_minutes: int) -> datetime:
    # Round up to next step boundary (e.g. 15 mins)
    dt = dt.replace(second=0, microsecond=0)
    mod = dt.minute % step_minutes
    if mod == 0:
        return dt
    return dt + timedelta(minutes=(step_minutes - mod))


def within_hours(
    dt: datetime,
    tz: ZoneInfo,
    open_days={"mon", "tue", "wed", "thu", "fri", "sat"},
    open_time=dtime(9, 0),
    close_time=dtime(18, 0),
) -> bool:
    local = dt.astimezone(tz)
    day = local.strftime("%a").lower()[:3]
    if day not in open_days:
        return False
    return open_time <= local.time() < close_time


def end_within_hours(
    start_dt: datetime,
    end_dt: datetime,
    tz: ZoneInfo,
    open_time=dtime(9, 0),
    close_time=dtime(18, 0),
) -> bool:
    # Ensure the full service fits before closing time (same day)
    s = start_dt.astimezone(tz)
    e = end_dt.astimezone(tz)
    if s.date() != e.date():
        return False
    return (open_time <= s.time()) and (e.time() <= close_time)


# ----------------------------
# Busy times (FreeBusy)
# ----------------------------

def get_busy_times(calendar_id: str, start: datetime, end: datetime, timezone: str):
    """
    Returns list of (busy_start, busy_end) in the requested timezone.
    Uses FreeBusy which includes all events.
    """
    service = get_service()
    tz = ZoneInfo(timezone)

    start = start.astimezone(tz) if start.tzinfo else start.replace(tzinfo=tz)
    end = end.astimezone(tz) if end.tzinfo else end.replace(tzinfo=tz)

    body = {
        "timeMin": start.isoformat(),
        "timeMax": end.isoformat(),
        "items": [{"id": calendar_id}],
    }

    result = service.freebusy().query(body=body).execute()
    busy = result["calendars"].get(calendar_id, {}).get("busy", [])

    intervals = []
    for b in busy:
        bs = datetime.fromisoformat(b["start"].replace("Z", "+00:00")).astimezone(tz)
        be = datetime.fromisoformat(b["end"].replace("Z", "+00:00")).astimezone(tz)
        intervals.append((bs, be))

    return intervals


# ----------------------------
# Availability API (used by WhatsApp_bot.py)
# ----------------------------

def is_time_available(
    start_dt: datetime,
    calendar_id: str,
    duration_minutes: int,
    timezone: str,
):
    tz = ZoneInfo(timezone)

    start_dt = start_dt.astimezone(tz) if start_dt.tzinfo else start_dt.replace(tzinfo=tz)
    end_dt = start_dt + timedelta(minutes=duration_minutes)

    # Full service must be within shop hours
    if not within_hours(start_dt, tz) or not end_within_hours(start_dt, end_dt, tz):
        return False, "Outside opening hours"

    busy = get_busy_times(calendar_id, start_dt, end_dt, timezone)

    for bs, be in busy:
        if overlaps(start_dt, end_dt, bs, be):
            return False, f"Clashes with {bs.strftime('%H:%M')}–{be.strftime('%H:%M')}"

    return True, "OK"


def next_available_slots(
    from_dt: datetime,
    calendar_id: str,
    duration_minutes: int,
    timezone: str,
    step_minutes: int = 15,
    max_results: int = 5,
    search_days: int = 7,
):
    """
    Returns next available start times (datetime list) after from_dt.
    Only returns slots where the FULL service fits and does not overlap busy times.
    """
    tz = ZoneInfo(timezone)
    from_dt = from_dt.astimezone(tz) if from_dt.tzinfo else from_dt.replace(tzinfo=tz)
    from_dt = ceil_to_step(from_dt, step_minutes)

    results = []
    cursor = from_dt
    end_search = from_dt + timedelta(days=search_days)

    open_days = {"mon", "tue", "wed", "thu", "fri", "sat"}
    open_time = dtime(9, 0)
    close_time = dtime(18, 0)

    while cursor < end_search and len(results) < max_results:
        day = cursor.strftime("%a").lower()[:3]

        # Closed day -> jump to next day 9am
        if day not in open_days:
            cursor = (cursor + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
            continue

        # Before opening -> jump to opening
        if cursor.time() < open_time:
            cursor = cursor.replace(hour=9, minute=0, second=0, microsecond=0)

        # After close -> jump to next day opening
        if cursor.time() >= close_time:
            cursor = (cursor + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
            continue

        slot_start = cursor
        slot_end = slot_start + timedelta(minutes=duration_minutes)

        # Must fully fit before close
        if not end_within_hours(slot_start, slot_end, tz, open_time=open_time, close_time=close_time):
            cursor = (cursor + timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
            continue

        busy = get_busy_times(calendar_id, slot_start, slot_end, timezone)
        clash = any(overlaps(slot_start, slot_end, bs, be) for bs, be in busy)

        if not clash:
            results.append(slot_start)

        cursor = cursor + timedelta(minutes=step_minutes)

    return results


# ----------------------------
# Event creation / deletion
# ----------------------------

def create_booking_event(
    calendar_id: str,
    service_name: str,
    customer_name: str,
    start_dt: datetime,
    duration_minutes: int,
    phone: str,
    timezone: str,
):
    tz = ZoneInfo(timezone)

    start_dt = start_dt.astimezone(tz) if start_dt.tzinfo else start_dt.replace(tzinfo=tz)
    end_dt = start_dt + timedelta(minutes=duration_minutes)

    body = {
        "summary": f"{service_name} — {customer_name}",
        "description": f"Customer: {customer_name}\nPhone: {phone}",
        "start": {"dateTime": start_dt.isoformat(), "timeZone": timezone},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": timezone},
    }

    service = get_service()
    event = service.events().insert(calendarId=calendar_id, body=body).execute()

    return {"event_id": event.get("id"), "html_link": event.get("htmlLink")}


def delete_booking_event(calendar_id: str, event_id: str):
    service = get_service()
    service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
    return True
