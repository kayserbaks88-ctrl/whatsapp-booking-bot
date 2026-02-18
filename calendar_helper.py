import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build

TIMEZONE = os.getenv("TIMEZONE_HINT", "Europe/London")
TZ = ZoneInfo(TIMEZONE)

SCOPES = ["https://www.googleapis.com/auth/calendar"]

def _get_service():
    # Use ONE of these in env:
    # GOOGLE_CREDENTIALS_JSON = full JSON string
    # OR GOOGLE_APPLICATION_CREDENTIALS = path to json file
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()
    creds_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()

    if creds_json:
        import json
        info = json.loads(creds_json)
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        if not creds_path:
            raise RuntimeError("Missing GOOGLE_CREDENTIALS_JSON or GOOGLE_APPLICATION_CREDENTIALS")
        creds = service_account.Credentials.from_service_account_file(creds_path, scopes=SCOPES)

    return build("calendar", "v3", credentials=creds, cache_discovery=False)

def is_time_available(calendar_id: str, start_dt: datetime, minutes: int) -> bool:
    svc = _get_service()
    end_dt = start_dt + timedelta(minutes=minutes)

    body = {
        "timeMin": start_dt.isoformat(),
        "timeMax": end_dt.isoformat(),
        "timeZone": str(TZ),
        "items": [{"id": calendar_id}],
    }
    fb = svc.freebusy().query(body=body).execute()
    busy = fb["calendars"][calendar_id].get("busy", [])
    return len(busy) == 0

def next_available_slots(calendar_id: str, minutes: int, start_from: datetime, tz: ZoneInfo, step_mins: int = 15, limit: int = 3):
    slots = []
    cur = start_from.astimezone(tz).replace(second=0, microsecond=0)

    # check up to ~7 days ahead
    for _ in range(int((7 * 24 * 60) / step_mins)):
        if is_time_available(calendar_id, cur, minutes):
            slots.append(cur)
            if len(slots) >= limit:
                break
        cur = cur + timedelta(minutes=step_mins)
    return slots

def create_booking_event(calendar_id: str, start_dt: datetime, end_dt: datetime, summary: str, description: str, phone: str, service_name: str):
    svc = _get_service()
    event = {
        "summary": summary,
        "description": f"{description}\nPhone: {phone}\nService: {service_name}",
        "start": {"dateTime": start_dt.isoformat(), "timeZone": str(TZ)},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": str(TZ)},
    }
    return svc.events().insert(calendarId=calendar_id, body=event).execute()
