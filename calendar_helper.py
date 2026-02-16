import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build


def _get_service():
    """
    Uses GOOGLE_CREDENTIALS_JSON (a JSON string) from env.
    """
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()
    if not creds_json:
        raise RuntimeError("Missing GOOGLE_CREDENTIALS_JSON env var")

    import json
    info = json.loads(creds_json)

    creds = service_account.Credentials.from_service_account_info(
        info,
        scopes=["https://www.googleapis.com/auth/calendar"],
    )
    return build("calendar", "v3", credentials=creds)


def _to_rfc3339(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return dt.isoformat()


def is_time_available(calendar_id: str, start_dt: datetime, end_dt: datetime) -> bool:
    svc = _get_service()
    body = {
        "timeMin": _to_rfc3339(start_dt),
        "timeMax": _to_rfc3339(end_dt),
        "items": [{"id": calendar_id}],
    }
    res = svc.freebusy().query(body=body).execute()
    busy = res.get("calendars", {}).get(calendar_id, {}).get("busy", [])
    return len(busy) == 0


def next_available_slots(
    calendar_id: str,
    day_start: datetime,
    day_end: datetime,
    duration_minutes: int,
    step_minutes: int = 15,
    limit: int = 3,
):
    """
    Returns list[datetime] start times for next available slots within [day_start, day_end].
    """
    slots = []
    cursor = day_start

    while cursor + timedelta(minutes=duration_minutes) <= day_end:
        end = cursor + timedelta(minutes=duration_minutes)
        if is_time_available(calendar_id, cursor, end):
            slots.append(cursor)
            if len(slots) >= limit:
                break
        cursor += timedelta(minutes=step_minutes)

    return slots


def create_booking_event(
    calendar_id: str,
    start_dt: datetime,
    end_dt: datetime,
    summary: str,
    description: str = "",
    phone: str | None = None,
    service_name: str | None = None,
):
    """
    phone/service_name are OPTIONAL, so WhatsApp_bot can pass them safely.
    """
    svc = _get_service()

    desc_lines = []
    if description:
        desc_lines.append(description)
    if service_name:
        desc_lines.append(f"Service: {service_name}")
    if phone:
        desc_lines.append(f"Customer: {phone}")

    event = {
        "summary": summary,
        "description": "\n".join(desc_lines).strip(),
        "start": {"dateTime": _to_rfc3339(start_dt)},
        "end": {"dateTime": _to_rfc3339(end_dt)},
    }

    created = svc.events().insert(calendarId=calendar_id, body=event).execute()
    return created
