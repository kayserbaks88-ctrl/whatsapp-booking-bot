import os
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials

TIMEZONE_HINT = os.getenv("TIMEZONE_HINT", "Europe/London").strip()
TZ = ZoneInfo(TIMEZONE_HINT)

GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "").strip()
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()

SCOPES = ["https://www.googleapis.com/auth/calendar"]

def _calendar_service():
    if not GOOGLE_CALENDAR_ID:
        raise RuntimeError("Missing GOOGLE_CALENDAR_ID")
    if not GOOGLE_CREDENTIALS_JSON:
        raise RuntimeError("Missing GOOGLE_CREDENTIALS_JSON")

    info = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)

def _to_rfc3339(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ).isoformat()

def is_time_available(start_dt: datetime, duration_min: int = 30) -> bool:
    svc = _calendar_service()
    end_dt = start_dt + timedelta(minutes=duration_min)

    fb = svc.freebusy().query(body={
        "timeMin": _to_rfc3339(start_dt),
        "timeMax": _to_rfc3339(end_dt),
        "items": [{"id": GOOGLE_CALENDAR_ID}]
    }).execute()

    busy = fb.get("calendars", {}).get(GOOGLE_CALENDAR_ID, {}).get("busy", [])
    return len(busy) == 0

def next_available_slots(start_dt: datetime, duration_min: int = 30, count: int = 3):
    """Simple forward search in 15-min steps."""
    slots = []
    cur = start_dt
    step = timedelta(minutes=15)

    # search up to ~7 days ahead
    limit = start_dt + timedelta(days=7)

    while cur < limit and len(slots) < count:
        if is_time_available(cur, duration_min=duration_min):
            slots.append(cur)
        cur += step

    return slots

def create_booking_event(start_dt: datetime, duration_min: int, service_name: str, phone: str, price=None):
    svc = _calendar_service()
    end_dt = start_dt + timedelta(minutes=duration_min)

    summary = service_name
    if price is not None and price != 0:
        summary = f"{service_name} (£{price})"

    event = {
        "summary": summary,
        "start": {"dateTime": _to_rfc3339(start_dt), "timeZone": TIMEZONE_HINT},
        "end": {"dateTime": _to_rfc3339(end_dt), "timeZone": TIMEZONE_HINT},
        "extendedProperties": {
            "private": {
                "phone": phone
            }
        }
    }

    created = svc.events().insert(calendarId=GOOGLE_CALENDAR_ID, body=event).execute()
    return created

def list_bookings_for_phone(phone: str):
    svc = _calendar_service()
    now = datetime.now(TZ)
    time_min = _to_rfc3339(now)

    events = svc.events().list(
        calendarId=GOOGLE_CALENDAR_ID,
        timeMin=time_min,
        singleEvents=True,
        orderBy="startTime",
        maxResults=20
    ).execute()

    out = []
    for e in events.get("items", []):
        priv = (e.get("extendedProperties") or {}).get("private") or {}
        if priv.get("phone") != phone:
            continue

        start = e.get("start", {}).get("dateTime")
        if not start:
            continue
        start_dt = datetime.fromisoformat(start).astimezone(TZ)

        out.append({
            "event_id": e["id"],
            "summary": e.get("summary", "Booking"),
            "start": start_dt,
        })

    return out

def cancel_booking_by_index(phone: str, index: int):
    bookings = list_bookings_for_phone(phone)
    if index < 1 or index > len(bookings):
        return False, "Invalid selection"

    svc = _calendar_service()
    eid = bookings[index - 1]["event_id"]
    svc.events().delete(calendarId=GOOGLE_CALENDAR_ID, eventId=eid).execute()
    return True, "Cancelled"

def reschedule_booking_by_index(phone: str, index: int, new_start_dt: datetime, duration_min: int = 30):
    bookings = list_bookings_for_phone(phone)
    if index < 1 or index > len(bookings):
        return False, "Invalid selection"

    svc = _calendar_service()
    eid = bookings[index - 1]["event_id"]

    # Fetch event to preserve summary & phone tagging
    event = svc.events().get(calendarId=GOOGLE_CALENDAR_ID, eventId=eid).execute()

    new_end_dt = new_start_dt + timedelta(minutes=duration_min)
    event["start"] = {"dateTime": _to_rfc3339(new_start_dt), "timeZone": TIMEZONE_HINT}
    event["end"] = {"dateTime": _to_rfc3339(new_end_dt), "timeZone": TIMEZONE_HINT}

    updated = svc.events().update(calendarId=GOOGLE_CALENDAR_ID, eventId=eid, body=event).execute()
    return True, updated.get("id")