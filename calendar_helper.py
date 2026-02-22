import os
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build

TIMEZONE = os.getenv("TIMEZONE_HINT", "Europe/London")
TZ = ZoneInfo(TIMEZONE)

GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()

def _get_service():
    if not GOOGLE_CREDENTIALS_JSON:
        raise RuntimeError("GOOGLE_CREDENTIALS_JSON is missing.")
    info = json.loads(GOOGLE_CREDENTIALS_JSON)
    creds = service_account.Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/calendar"],
    )
    return build("calendar", "v3", credentials=creds, cache_discovery=False)

def is_time_available(calendar_id: str, start_dt: datetime, end_dt: datetime) -> bool:
    """Returns True if no events overlap [start_dt, end_dt)."""
    svc = _get_service()
    events = svc.events().list(
        calendarId=calendar_id,
        timeMin=start_dt.astimezone(TZ).isoformat(),
        timeMax=end_dt.astimezone(TZ).isoformat(),
        singleEvents=True,
        orderBy="startTime",
        maxResults=50,
    ).execute()

    for e in events.get("items", []):
        s = e.get("start", {}).get("dateTime")
        t = e.get("end", {}).get("dateTime")
        if not s or not t:
            continue
        es = datetime.fromisoformat(s)
        ee = datetime.fromisoformat(t)
        # overlap test
        if start_dt < ee and end_dt > es:
            return False
    return True

def next_available_slots(calendar_id: str, duration_minutes: int, start_from: datetime, step_minutes: int = 15, limit: int = 3):
    """Find next 'limit' available slots from start_from onward."""
    slots = []
    cursor = start_from.astimezone(TZ)

    # Search ahead up to 21 days (safe guard)
    end_search = cursor + timedelta(days=21)

    while cursor < end_search and len(slots) < limit:
        end_dt = cursor + timedelta(minutes=duration_minutes)
        if is_time_available(calendar_id, cursor, end_dt):
            slots.append(cursor)
        cursor += timedelta(minutes=step_minutes)

    return slots

def create_booking_event(calendar_id: str, start_dt: datetime, duration_minutes: int, service_name: str, customer_phone: str):
    svc = _get_service()
    end_dt = start_dt + timedelta(minutes=duration_minutes)

    body = {
        "summary": f"{service_name}",
        "description": f"TrimTech Booking\nWA:{customer_phone}\nService:{service_name}",
        "start": {"dateTime": start_dt.astimezone(TZ).isoformat()},
        "end": {"dateTime": end_dt.astimezone(TZ).isoformat()},
    }

    created = svc.events().insert(calendarId=calendar_id, body=body).execute()
    return {
        "id": created.get("id"),
        "htmlLink": created.get("htmlLink"),
        "start": start_dt,
        "end": end_dt,
    }

def delete_booking_event(calendar_id: str, event_id: str) -> bool:
    svc = _get_service()
    svc.events().delete(calendarId=calendar_id, eventId=event_id).execute()
    return True

def update_booking_time(calendar_id: str, event_id: str, new_start: datetime, duration_minutes: int):
    svc = _get_service()
    ev = svc.events().get(calendarId=calendar_id, eventId=event_id).execute()

    new_end = new_start + timedelta(minutes=duration_minutes)
    ev["start"]["dateTime"] = new_start.astimezone(TZ).isoformat()
    ev["end"]["dateTime"] = new_end.astimezone(TZ).isoformat()

    updated = svc.events().update(calendarId=calendar_id, eventId=event_id, body=ev).execute()
    return {
        "id": updated.get("id"),
        "htmlLink": updated.get("htmlLink"),
        "start": new_start,
        "end": new_end,
        "summary": updated.get("summary", ""),
    }

def list_user_bookings(calendar_id: str, customer_phone: str, limit: int = 10):
    """Lists upcoming bookings by searching description for WA:<phone>."""
    svc = _get_service()
    now = datetime.now(TZ)
    events = svc.events().list(
        calendarId=calendar_id,
        timeMin=now.isoformat(),
        singleEvents=True,
        orderBy="startTime",
        q=f"WA:{customer_phone}",
        maxResults=limit,
    ).execute()

    results = []
    for e in events.get("items", []):
        s = e.get("start", {}).get("dateTime")
        if not s:
            continue
        dt = datetime.fromisoformat(s).astimezone(TZ)
        results.append({
            "id": e.get("id"),
            "when": dt,
            "service": e.get("summary", "Booking"),
            "htmlLink": e.get("htmlLink"),
        })
    return results