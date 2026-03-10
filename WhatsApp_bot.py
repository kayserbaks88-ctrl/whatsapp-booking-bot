from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse

from llm_helper import llm_extract
from calendar_helper import is_free, create_booking

from datetime import datetime
from zoneinfo import ZoneInfo
import dateparser

app = Flask(__name__)

PENDING = {}

SERVICES = {
    "haircut": ("Haircut", 18),
    "skin fade": ("Skin Fade", 22),
    "beard trim": ("Beard Trim", 10),
    "shape up": ("Shape Up", 12)
}


@app.route("/whatsapp", methods=["POST"])
def whatsapp():

    incoming = request.values.get("Body", "").strip()
    number = request.values.get("From")

    resp = MessagingResponse()
    msg = resp.message()

    data = llm_extract(incoming)

    intent = data.get("intent")
    service = data.get("service")
    time_text = data.get("time")

    timezone = ZoneInfo("Europe/London")

    if time_text:
        time = dateparser.parse(
            time_text,
            settings={
                "TIMEZONE": "Europe/London",
                "RETURN_AS_TIMEZONE_AWARE": True,
                "PREFER_DATES_FROM": "future"
            }
        )
    else:
        time = None

    # Greeting
    if intent == "greeting":

        msg.body(
            "Hi! 👋 Welcome to BBC Barbers.\n\n"
            "How can I help today?"
        )

        return str(resp)

    # Thanks
    if intent == "thanks":

        msg.body(
            "You're very welcome! 😊\n\n"
            "Just message anytime if you need another appointment."
        )

        return str(resp)

    # Availability question
    if intent == "availability":

        msg.body(
            "Sure 👍 What day are you looking for?"
        )

        return str(resp)

    # Continue booking
    if number in PENDING:

        booking = PENDING[number]

        if not booking.get("time") and time:

            if not is_free(time):

                msg.body(
                    "Ah sorry — that slot has just gone ❌\n\n"
                    "Could you try another time?"
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

        if booking.get("time"):

            name = incoming

            create_booking(
                name,
                booking["service"],
                booking["price"],
                booking["time"]
            )

            msg.body(
                f"✅ You're all booked {name}!\n\n"
                f"{booking['service']}\n"
                f"{booking['time'].strftime('%A %H:%M')}\n\n"
                "See you soon 💈"
            )

            del PENDING[number]

            return str(resp)

    # New booking request
    if intent == "booking":

        if service and service in SERVICES:

            service_name, price = SERVICES[service]

            if not time:

                PENDING[number] = {
                    "service": service_name,
                    "price": price
                }

                msg.body(
                    f"Great choice! {service_name} is £{price}.\n\n"
                    "What time would you like?"
                )

                return str(resp)

            if not is_free(time):

                msg.body(
                    "Ah sorry — that time is already booked.\n\n"
                    "Could you try another?"
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

        else:

            msg.body(
                "Sure 🙂 What service would you like?\n\n"
                "✂️ Haircut\n🔥 Skin Fade\n🧔 Beard Trim\n🪒 Shape Up"
            )

            return str(resp)

    msg.body(
        "Hi 👋 How can I help today?"
    )

    return str(resp)


if __name__ == "__main__":
    app.run()