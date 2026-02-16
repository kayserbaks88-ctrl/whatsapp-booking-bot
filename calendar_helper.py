import os
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/calendar"]


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


# ---------- HELPERS ----------

def overlaps(a_start, a_end, b_start, b_end):
    return a_start < b_end and a_end > b_start


def _to_tz(dt: datetime, tz: ZoneInfo) -> datetime:
    return dt.astimezone(tz) if dt.tzinfo else dt.replace(tzinfo=tz)


# ---------- AVAILABILITY (FREEBUSY) ----------

def get_busy_times(calendar_id: str, start: datetime, end: datetime, timezone: str):
    """
    Returns list of (busy_start, busy_end) in the requested timezone.
    Uses FreeBusy which includes all events.
    """
    service = get_service()
    tz = ZoneInfo(timezone)

    start = _to_tz(start, tz)
    end = _to_tz(end, tz)

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


def is_time_available(
    start_dt: datetime,
    calendar_id: str,
    duration_minutes: int,
    timezone: str,
    buffer_minutes: int = 0,
    ignore_interval=None,
):
    """
    Checks if [start_dt, start_dt+duration+buffer] overlaps any busy time.
    ignore_interval: (old_start, old_end) to ignore clashes with the booking being rescheduled.
    """
    tz = ZoneInfo(timezone)
    start_dt = _to_tz(start_dt, tz)

    end_dt = start_dt + timedelta(minutes=duration_minutes + max(0, buffer_minutes))

    busy = get_busy_times(calendar_id, start_dt, end_dt, timezone)

    ign_s = ign_e = None
    if ignore_interval:
        ign_s = _to_tz(ignore_interval[0], tz)
        ign_e = _to_tz(ignore_interval[1], tz)

    for bs, be in busy:
        if ign_s and ign_e and bs == ign_s and be == ign_e:
            continue

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
    buffer_minutes: int = 0,
):
    """
    Finds next available start times after from_dt.
    NOTE: opening hours should be enforced in WhatsApp_bot.py;
          this function only finds non-overlapping slots.
    """
    tz = ZoneInfo(timezone)
    from_dt = _to_tz(from_dt, tz).replace(second=0, microsecond=0)

    mod = from_dt.minute % step_minutes
    if mod != 0:
        from_dt = from_dt + timedelta(minutes=(step_minutes - mod))
        from_dt = from_dt.replace(second=0, microsecond=0)

    results = []
    cursor = from_dt
    end_search = from_dt + timedelta(days=search_days)

    while cursor < end_search and len(results) < max_results:
        ok, _ = is_time_available(
            cursor,
            calendar_id,
            duration_minutes,
            timezone,
            buffer_minutes=buffer_minutes,
        )
        if ok:
            results.append(cursor)

        cursor += timedelta(minutes=step_minutes)

    return results


# ---------- EVENTS ----------

def create_booking_event(
    calendar_id: str,
    service_name: str,
    customer_name: str,
    start_dt: datetime,
    duration_minutes: int,
    phone: str = "",
    timezone: str = "Europe/London",
    **_ignored_kwargs,  # IMPORTANT: keeps compatibility even if caller passes extra args
):
    """
    Create booking event.
    Accepts phone (and ignores any unexpected kwargs) to prevent deployment mismatch errors.
    """
    tz = ZoneInfo(timezone)
    start_dt = _to_tz(start_dt, tz)
    end_dt = start_dt + timedelta(minutes=duration_minutes)

    desc_lines = [f"Customer: {customer_name}"]
    if phone:
        desc_lines.append(f"Phone: {phone}")

    body = {
        "summary": f"{service_name} — {customer_name}",
        "description": "\n".join(desc_lines),
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
