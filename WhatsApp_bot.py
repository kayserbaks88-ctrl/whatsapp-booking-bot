import os
import dateparser

from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse

from llm_helper import llm_extract
from calendar_helper import is_free, create_booking

from zoneinfo import ZoneInfo


app = Flask(__name__)

TIMEZONE = ZoneInfo("Europe/London")


@app.route("/whatsapp", methods=["POST"])
def whatsapp():

    incoming = request.values.get("Body", "").strip()
    number = request.values.get("From")

    resp = MessagingResponse()
    reply = resp.message()

    data = llm_extract(incoming)

    intent = data.get("intent")
    service = data.get("service")
    when_text = data.get("when_text")

    text = incoming.lower()

    # fallback if AI fails
    if not intent:
        if "haircut" in text or "fade" in text or "trim" in text:
            intent = "book"
            service = "haircut"

    if intent != "book":
        reply.body("Hi 👋 How can I help today?")
        return str(resp)

    if not when_text:
        reply.body("What time would you like your haircut?")
        return str(resp)

    time = dateparser.parse(
        when_text,
        settings={
            "TIMEZONE": "Europe/London",
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DATES_FROM": "future"
        }
    )

    if not time:
        reply.body("Sorry I couldn't understand the time.")
        return str(resp)

    time = time.astimezone(TIMEZONE)

    end_time = time + dateparser.timedelta(minutes=30)

    if not is_free(time, end_time):

        reply.body("Sorry that slot is taken. Try another time.")
        return str(resp)

    create_booking(number, service, time)

    reply.body(
        f"✅ {service.title()} booked for {time.strftime('%A %H:%M')}"
    )

    return str(resp)


if __name__ == "__main__":
    app.run()