import os
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from google.oauth2 import service_account
from googleapiclient.discovery import build


def _get_calendar_service():
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip()
    if not creds_json:
        raise RuntimeError("Missing GOOGLE_CREDENTIALS_JSON env var")

    info = json.loads(creds_json)
    scopes = ["https://www.googleapis.com/auth/calendar"]
    creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def is_time_available(calendar_id: str, start_dt: datetime, duration_minutes: int, tz: ZoneInfo, ignore_event_id: str = None) -> bool:
    service = _get_calendar_service()
    end_dt = start_dt + timedelta(minutes=duration_minutes)

    events = service.events().list(
        calendarId=calendar_id,
        timeMin=start_dt.astimezone(tz).isoformat(),
        timeMax=end_dt.astimezone(tz).isoformat(),
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    for ev in events.get("items", []):
        if ignore_event_id and ev.get("id") == ignore_event_id:
            continue
        # any overlap means not available (timeMin/timeMax window already restricts)
        return False

    return True


def next_available_slots(calendar_id: str, desired_dt: datetime, duration_minutes: int, tz: ZoneInfo, limit: int = 3, step_minutes: int = 15):
    slots = []
    probe = desired_dt

    # look forward up to ~7 days max
    for _ in range(int((7 * 24 * 60) / step_minutes)):
        if is_time_available(calendar_id, probe, duration_minutes, tz):
            slots.append(probe)
            if len(slots) >= limit:
                break
        probe = probe + timedelta(minutes=step_minutes)

    return slots


def create_booking_event(calendar_id: str, from_number: str, service_name: str, start_dt: datetime, duration_minutes: int, tz: ZoneInfo, shop_name: str):
    service = _get_calendar_service()
    end_dt = start_dt + timedelta(minutes=duration_minutes)

    # Tag WhatsApp number in description for filtering
    description = f"Booking via WhatsApp\nCustomer: {from_number}\nService: {service_name}"

    body = {
        "summary": f"{service_name} ({from_number})",
        "description": description,
        "start": {"dateTime": start_dt.astimezone(tz).isoformat(), "timeZone": str(tz)},
        "end": {"dateTime": end_dt.astimezone(tz).isoformat(), "timeZone": str(tz)},
        "location": shop_name,
    }

    created = service.events().insert(calendarId=calendar_id, body=body).execute()

    return {
        "event_id": created.get("id"),
        "htmlLink": created.get("htmlLink"),
    }


def list_upcoming_bookings_for_number(calendar_id: str, from_number: str, tz: ZoneInfo, limit: int = 10):
    service = _get_calendar_service()
    now = datetime.now(tz)

    events = service.events().list(
        calendarId=calendar_id,
        timeMin=now.isoformat(),
        maxResults=50,
        singleEvents=True,
        orderBy="startTime",
    ).execute()

    results = []
    for ev in events.get("items", []):
        desc = (ev.get("description") or "")
        if from_number not in desc and from_number not in (ev.get("summary") or ""):
            continue

        start_str = ev["start"].get("dateTime")
        if not start_str:
            continue
        start_dt = datetime.fromisoformat(start_str).astimezone(tz)

        # service name from summary before "("
        summary = ev.get("summary") or ""
        service_name = summary.split("(")[0].strip() if summary else "Booking"

        results.append({
            "event_id": ev.get("id"),
            "service": service_name,
            "start": start_dt,
            "htmlLink": ev.get("htmlLink"),
        })

    # sort and limit
    results.sort(key=lambda x: x["start"])
    return results[:limit]


def delete_booking_event(calendar_id: str, event_id: str):
    service = _get_calendar_service()
    service.events().delete(calendarId=calendar_id, eventId=event_id).execute()


def reschedule_booking_event(calendar_id: str, event_id: str, new_start_dt: datetime, duration_minutes: int, tz: ZoneInfo):
    service = _get_calendar_service()
    ev = service.events().get(calendarId=calendar_id, eventId=event_id).execute()

    new_end_dt = new_start_dt + timedelta(minutes=duration_minutes)
    ev["start"] = {"dateTime": new_start_dt.astimezone(tz).isoformat(), "timeZone": str(tz)}
    ev["end"] = {"dateTime": new_end_dt.astimezone(tz).isoformat(), "timeZone": str(tz)}

    updated = service.events().update(calendarId=calendar_id, eventId=event_id, body=ev).execute()
    return updated