import os
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from datetime import timedelta
from zoneinfo import ZoneInfo

from llm_helper import llm_extract
from calendar_helper import is_free, create_booking

app = Flask(__name__)

TZ = ZoneInfo("Europe/London")

PENDING = {}

WELCOME = """
💈 BBC Barbers

Send a message like:

Haircut tomorrow 2pm
Skin fade Friday 3pm
Beard trim Monday 5

Prices:

✂️ Haircut — £18
🔥 Skin Fade — £22
📏 Shape Up — £12
🧔 Beard Trim — £10
🪒 Hot Towel Shave — £25
💨 Blow Dry — £20
"""


@app.route("/whatsapp", methods=["POST"])
def whatsapp():

    resp = MessagingResponse()
    msg = resp.message()

    from_number = request.values.get("From", "")
    text = request.values.get("Body", "").strip()

    profile_name = request.values.get("ProfileName", "")

    if not profile_name:
        profile_name = from_number

    state = PENDING.get(from_number)

    if state and state["step"] == "await_name":

        state["name"] = text

        msg.body(
            f"Confirm booking:\n\n"
            f"{state['service'][0]} — £{state['service'][1]}\n"
            f"{state['time'].strftime('%A %H:%M')}\n"
            f"Name: {text}\n\n"
            f"Reply YES to confirm."
        )

        state["step"] = "confirm"

        return str(resp)

    if state and state["step"] == "confirm":

        if text.lower() != "yes":
            msg.body("Booking cancelled.")
            PENDING.pop(from_number, None)
            return str(resp)

        service = state["service"]
        start_dt = state["time"]
        name = state["name"]

        if not is_free(start_dt, 30):

            msg.body("❌ That time is taken. Try another time.")
            PENDING.pop(from_number, None)
            return str(resp)

        booking = create_booking(
            phone=from_number,
            service_name=service[0],
            start_dt=start_dt,
            minutes=30,
            name=name
        )

        link = booking.get("html_link", "")

        msg.body(
            f"✅ Booked!\n\n"
            f"{service[0]} — £{service[1]}\n"
            f"{start_dt.strftime('%A %H:%M')}\n"
            f"👤 {name}\n\n"
            f"📅 Add to calendar:\n{link}"
        )

        PENDING.pop(from_number, None)

        return str(resp)

    data = llm_extract(text)

    if data["service"] and data["datetime"]:

        PENDING[from_number] = {
            "service": data["service"],
            "time": data["datetime"].astimezone(TZ),
            "step": "await_name"
        }

        msg.body("What name should I book this under?")

        return str(resp)

    msg.body(WELCOME)

    return str(resp)


if __name__ == "__main__":
    app.run()