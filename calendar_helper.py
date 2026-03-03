import os
import json
from datetime import datetime, timedelta
from typing import List, Dict, Any

from google.oauth2 import service_account
from googleapiclient.discovery import build

TIMEZONE = os.getenv("TIMEZONE", "Europe/London").strip()
CALENDAR_ID = (os.getenv("GOOGLE_CALENDAR_ID") or "").strip()

# IMPORTANT: your Render env must be exactly GOOGLE_SERVICE_ACCOUNT_JSON
SA_JSON = (os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") or "").strip()

SCOPES = ["https://www.googleapis.com/auth/calendar"]


def _svc():
    if not SA_JSON:
        raise RuntimeError("Missing GOOGLE_SERVICE_ACCOUNT_JSON")

    try:
        info = json.loads(SA_JSON)
    except Exception as e:
        raise RuntimeError(f"Invalid GOOGLE_SERVICE_ACCOUNT_JSON (not JSON): {e}")

    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def list_upcoming(phone: str, limit: int = 10) -> List[Dict[str, Any]]:
    """List upcoming events tagged with this phone in extendedProperties.private.phone"""
    if not CALENDAR_ID:
        return []

    service = _svc()
    now = datetime.utcnow().isoformat() + "Z"

    resp = service.events().list(
        calendarId=CALENDAR_ID,
        timeMin=now,
        maxResults=50,
        singleEvents=True,
        orderBy="startTime",
        privateExtendedProperty=f"phone={phone}",
    ).execute()

    items = resp.get("items", [])
    out: List[Dict[str, Any]] = []
    for ev in items[:limit]:
        out.append(
            {
                "id": ev.get("id"),
                "summary": ev.get("summary", ""),
                "start": (ev.get("start") or {}).get("dateTime") or (ev.get("start") or {}).get("date"),
                "end": (ev.get("end") or {}).get("dateTime") or (ev.get("end") or {}).get("date"),
                "link": ev.get("htmlLink", ""),
            }
        )
    return out


def is_free(start_dt: datetime, end_dt: datetime) -> bool:
    """Checks for conflicts."""
    if not CALENDAR_ID:
        return True

    service = _svc()
    body = {
        "timeMin": start_dt.isoformat(),
        "timeMax": end_dt.isoformat(),
        "timeZone": TIMEZONE,
        "items": [{"id": CALENDAR_ID}],
    }
    fb = service.freebusy().query(body=body).execute()
    busy = (fb.get("calendars", {}).get(CALENDAR_ID, {}) or {}).get("busy", [])
    return len(busy) == 0


def create_booking(phone: str, service_name: str, start_dt: datetime, minutes: int = 30) -> Dict[str, Any]:
    """
    Creates a calendar event and returns {ok, id, link}.
    link is the Google Calendar htmlLink (useful for admin/testing).
    """
    if not CALENDAR_ID:
        return {"ok": False, "error": "CALENDAR_NOT_SET"}

    service = _svc()
    end_dt = start_dt + timedelta(minutes=minutes)

    event = {
        "summary": f"{service_name} - {phone}",
        "start": {"dateTime": start_dt.isoformat(), "timeZone": TIMEZONE},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": TIMEZONE},
        "extendedProperties": {"private": {"phone": phone, "service": service_name}},
    }

    created = service.events().insert(calendarId=CALENDAR_ID, body=event).execute()

    # Render logs
    print("EVENT CREATED:", created.get("id"), created.get("htmlLink"), flush=True)

    return {
        "ok": True,
        "id": created.get("id", ""),
        "link": created.get("htmlLink", ""),
    }


def cancel_booking(event_id: str) -> bool:
    if not CALENDAR_ID:
        return False
    service = _svc()
    service.events().delete(calendarId=CALENDAR_ID, eventId=event_id).execute()
    return True