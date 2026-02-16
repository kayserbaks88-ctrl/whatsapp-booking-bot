import os
import re
import json
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

import dateparser
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from dotenv import load_dotenv

from calendar_helper import (
    create_booking_event,
    is_time_available,
    next_available_slots,
)

# ============================
# LOAD ENV
# ============================

load_dotenv()

BUSINESS_NAME = os.getenv("BUSINESS_NAME", "BBC Barbers")
TIMEZONE = os.getenv("TIMEZONE_HINT", "Europe/London")
TZ = ZoneInfo(TIMEZONE)

CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

PORT = int(os.getenv("PORT", "10000"))

# ============================
# CONFIG
# ============================

OPEN_TIME = time(9, 0)
CLOSE_TIME = time(18, 0)

SERVICES = {

    "haircut": 45,
    "skin fade": 60,
    "shape up": 30,
    "beard trim": 30,
    "kids cut": 45,

}

# ============================
# OPENAI CALL
# ============================

def call_openai_json(user_text):

    try:

        import requests

        url = "https://api.openai.com/v1/responses"

        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        }

        schema = {

            "type": "object",
            "properties": {

                "services": {
                    "type": "array",
                    "items": {"type": "string"}
                },

                "datetime_text": {
                    "type": "string"
                }

            },

            "required": ["services", "datetime_text"]

        }

        data = {

            "model": OPENAI_MODEL,

            "input": [

                {
                    "role": "system",
                    "content":
                    "Extract booking info from barber message."
                },

                {
                    "role": "user",
                    "content": user_text
                }

            ],

            "response_format": {

                "type": "json_schema",
                "json_schema": {
                    "name": "booking_extract",
                    "schema": schema
                }

            }

        }

        r = requests.post(url, headers=headers, json=data)

        result = r.json()

        text = result["output"][0]["content"][0]["text"]

        return json.loads(text)

    except Exception as e:

        print("LLM ERROR:", e)

        return None


# ============================
# MENU TEXT
# ============================

def menu():

    return (
        "💈 BBC Barbers\n\n"
        "Reply with number or name:\n\n"
        "1) Haircut\n"
        "2) Skin Fade\n"
        "3) Shape Up\n"
        "4) Beard Trim\n"
        "5) Kids Cut\n\n"
        "Example:\n"
        "Book haircut tomorrow 2pm"
    )


# ============================
# FLASK
# ============================

app = Flask(__name__)

# ============================
# MAIN ROUTE
# ============================

@app.route("/whatsapp", methods=["POST"])

def whatsapp():

    body = request.values.get("Body", "").lower()

    phone = request.values.get("From")

    resp = MessagingResponse()

    msg = resp.message()

    # ============================
    # LLM FIRST PRIORITY
    # ============================

    if OPENAI_API_KEY:

        extracted = call_openai_json(body)

        if extracted:

            services = extracted.get("services", [])

            dt_text = extracted.get("datetime_text")

            if services and dt_text:

                service = services[0].lower()

                if service not in SERVICES:

                    msg.body(menu())

                    return str(resp)

                dt = dateparser.parse(

                    dt_text,
                    settings={
                        "TIMEZONE": TIMEZONE,
                        "RETURN_AS_TIMEZONE_AWARE": True
                    }

                )

                if not dt:

                    msg.body("Could not understand time")

                    return str(resp)

                start = dt

                end = start + timedelta(minutes=SERVICES[service])

                create_booking_event(

                    CALENDAR_ID,
                    service,
                    start,
                    end,
                    timezone=TIMEZONE,
                    phone=phone

                )

                msg.body(

                    f"✅ Booked: {service.title()}\n"
                    f"📅 {start.strftime('%a %d %b %H:%M')}"

                )

                return str(resp)

    # ============================
    # MENU FALLBACK
    # ============================

    msg.body(menu())

    return str(resp)


# ============================
# RUN
# ============================

if __name__ == "__main__":

    app.run(host="0.0.0.0", port=PORT)
