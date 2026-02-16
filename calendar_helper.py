import os
import json
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar"]

def _get_calendar_service():
    raw = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()
    if not raw:
        raise RuntimeError("Missing GOOGLE_CREDENTIALS_JSON env var")

    info = json.loads(raw)
    creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    return build("calendar", "v3", credentials=creds)

def _to_rfc3339(dt: datetime):
    if dt.tzinfo is None:
        raise ValueError("Datetime must be timezone aware")
    return dt.isoformat()

def is_time_available(calendar_id: str, start_dt: datetime, duration_mins: int, tz: ZoneInfo) -> bool:
    svc = _get_calendar_service()
    if start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=tz)
    end_dt = start_dt + timedelta(minutes=duration_mins)

    events = svc.events().list(
        calendarId=calendar_id,
        timeMin=_to_rfc3339(start_dt),
        timeMax=_to_rfc3339(end_dt),
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    items = events.get("items", [])
    return len(items) == 0

def next_available_slots(
    calendar_id: str,
    tz: ZoneInfo,
    duration_mins: int,
    count: int = 3,
    step_mins: int = 15,
    open_days=None,
    open_time: time = time(9, 0),
    close_time: time = time(18, 0),
):
    now = datetime.now(tz)
    # start searching from next step
    cursor = (now + timedelta(minutes=step_mins)).replace(second=0, microsecond=0)
    out = []

    open_days = open_days or {"mon", "tue", "wed", "thu", "fri", "sat"}

    # search up to 30 days
    for _ in range(int((30 * 24 * 60) / step_mins)):
        dow = cursor.strftime("%a").lower()[:3]
        if dow in open_days and open_time <= cursor.time() <= close_time:
            end_dt = cursor + timedelta(minutes=duration_mins)
            if end_dt.time() <= close_time:
                if is_time_available(calendar_id, cursor, duration_mins, tz):
                    out.append(cursor)
                    if len(out) >= count:
                        return out
        cursor += timedelta(minutes=step_mins)
    return out

def create_booking_event(
    calendar_id: str,
    start_dt: datetime,
    end_dt: datetime,
    summary: str,
    description: str = "",
    phone: str | None = None,
    customer_name: str | None = None,
    service_name: str | None = None,
):
    """
    phone is optional and accepted so WhatsApp_bot.py never crashes.
    """
    svc = _get_calendar_service()

    lines = []
    if description:
        lines.append(description)
    if service_name:
        lines.append(f"Service: {service_name}")
    if customer_name:
        lines.append(f"Name: {customer_name}")
    if phone:
        lines.append(f"Phone: {phone}")

    body = {
        "summary": summary,
        "description": "\n".join(lines).strip(),
        "start": {"dateTime": _to_rfc3339(start_dt)},
        "end": {"dateTime": _to_rfc3339(end_dt)},
    }

    return svc.events().insert(calendarId=calendar_id, body=body).execute()
