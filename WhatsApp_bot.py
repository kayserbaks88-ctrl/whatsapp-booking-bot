from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse

from llm_helper import llm_extract
from calendar_helper import (
    is_free,
    create_booking,
    next_available_slots,
    find_available_slots
)

from datetime import datetime
from zoneinfo import ZoneInfo

app = Flask(__name__)

PENDING = {}

WELCOME = """
💈 *BBC Barbers*

Hi there! 👋

How can I help today?

You can book by sending something like:

✂️ Haircut tomorrow 2pm
🔥 Skin fade Friday 3pm
🧔 Beard trim Monday 5

Prices:

✂️ Haircut — £18
🔥 Skin Fade — £22
🪒 Shape Up — £12
🧔 Beard Trim — £10
🪓 Hot Towel Shave — £25
💨 Blow Dry — £20
"""


@app.route("/whatsapp", methods=["POST"])
def whatsapp():

    incoming = request.values.get("Body", "").strip()
    number = request.values.get("From")

    resp = MessagingResponse()
    msg = resp.message()

    service, time = llm_extract(incoming, "Europe/London")

    text = incoming.lower()

    # Greeting
    if text in ["hi", "hello", "hey"]:
        msg.body(
            "Hi! 👋 Welcome to *BBC Barbers*.\n\n"
            "How can I help today?"
        )
        return str(resp)

    # Thank you
    if text in ["thanks", "thank you", "cheers"]:
        msg.body(
            "You're very welcome! 😊\n\n"
            "Just message anytime if you need another appointment.\n"
            "Have a great day! 💈"
        )
        return str(resp)

    # Availability questions
    if any(word in text for word in ["available", "free", "slots"]):

        now = datetime.now(ZoneInfo("Europe/London"))

        slots = find_available_slots(now)

        options = "\n".join(
            slot.strftime("%A %H:%M") for slot in slots
        )

        msg.body(
            "Sure 👍 Here are the next available slots:\n\n"
            f"{options}\n\n"
            "Just tell me which one you'd like."
        )

        return str(resp)

    # Continue booking
    if number in PENDING:

        booking = PENDING[number]

        if "time" not in booking and time:

            if not is_free(time):

                suggestions = next_available_slots(time)

                options = "\n".join(
                    slot.strftime("%A %H:%M") for slot in suggestions
                )

                msg.body(
                    "Ah sorry — that slot has just gone ❌\n\n"
                    "Next available times:\n\n"
                    f"{options}"
                )

                return str(resp)

            booking["time"] = time

            msg.body(
                f"👍 That time is available!\n\n"
                f"{booking['service']}\n"
                f"{time.strftime('%A %H:%M')}\n\n"
                "What name should I put on the booking?"
            )

            return str(resp)

        if "time" in booking:

            name = incoming

            link = create_booking(
                name,
                booking["service"],
                booking["price"],
                booking["time"]
            )

            msg.body(
                f"✅ *All set, {name}!*\n\n"
                f"I've booked you in for:\n"
                f"✂️ {booking['service']}\n"
                f"📅 {booking['time'].strftime('%A %H:%M')}\n\n"
                f"📲 Add it to your calendar:\n{link}\n\n"
                f"If you need to change anything just message me.\n"
                f"See you soon! 💈"
            )

            del PENDING[number]

            return str(resp)

    # New booking
    if service:

        service_name, price = service

        if not time:

            PENDING[number] = {
                "service": service_name,
                "price": price
            }

            msg.body(
                f"💈 Great choice! A *{service_name}* is £{price}.\n\n"
                "What time would you like?"
            )

            return str(resp)

        if not is_free(time):

            suggestions = next_available_slots(time)

            options = "\n".join(
                slot.strftime("%A %H:%M") for slot in suggestions
            )

            msg.body(
                "Ah sorry — that slot has just gone ❌\n\n"
                "Next available times:\n\n"
                f"{options}"
            )

            return str(resp)

        PENDING[number] = {
            "service": service_name,
            "price": price,
            "time": time
        }

        msg.body(
            f"👍 That time is available!\n\n"
            f"{service_name}\n"
            f"{time.strftime('%A %H:%M')}\n\n"
            "What name should I put on the booking?"
        )

        return str(resp)

    msg.body(WELCOME)

    return str(resp)


if __name__ == "__main__":
    app.run()