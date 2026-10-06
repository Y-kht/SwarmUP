# Sending to the messaging apps (Telegram and WhatsApp), and checking what the user gave for them.
import json
import urllib.request

from harness_utils import ConnectionLost, fetchUrl, raiseIfOffline


TELEGRAM_API = "https://api.telegram.org"
WHATSAPP_API = "https://graph.facebook.com/v25.0"
API_ANSWER_LIMIT = 100000
MESSAGE_LIMIT = 4000


# ==============
# Messaging apps. A briefing can be sent to the phone of the user. MESSAGING_APPS in sources_library.py lists what each app needs.
# The tokens are only kept in memory, and they are never written in a message for the user.
# ==============
WHATSAPP_PROBLEMS = {
    131047: "WhatsApp only allows free text to someone who wrote to the app in the last 24 hours. Send any message to the number of your app, then try again.",
    190: "WhatsApp refused the access token. It may have expired, so create a new one.",
    100: "WhatsApp does not accept the phone number ID or the number. Check both.",
    131009: "WhatsApp does not accept the phone number ID or the number. Check both.",
    131026: "WhatsApp could not deliver the message. The number may not use WhatsApp.",
    131056: "WhatsApp says there are too many messages. Try again later.",
    130429: "WhatsApp says there are too many messages. Try again later.",
    80007: "WhatsApp says there are too many messages. Try again later.",
}


# A problem written for the user. It never contains a token.
class MessagingError(Exception):
    pass


def describeTelegramError(code, description):
    if code == 401:
        return "Telegram refused the bot token. Copy it again from @BotFather."
    if "chat not found" in description.lower():
        return "Telegram does not know this chat. Open your bot in Telegram, press Start, send it a message, and check the chat number."
    if code == 403:
        return "The bot is not allowed to write to this chat. Open the bot in Telegram and press Start."
    if code == 429:
        return "Telegram says there are too many messages. Try again later."
    return f"Telegram answered with error {code}: {description or 'no details'}"


def describeWhatsappError(error, code):
    number = error.get("code", code)
    details = (error.get("error_data") or {}).get("details") or error.get("message") or "no details"
    return WHATSAPP_PROBLEMS.get(number, f"WhatsApp answered with error {number}: {details}")


# One call to the API of a messaging app. It gives the HTTP code and the JSON answer ({} if there is none), because these APIs explain their errors in it.
def callApi(url, payload=None, headers=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = dict(headers or {})
    if data:
        headers["Content-Type"] = "application/json"
    try:
        code, answered = 200, fetchUrl(url, limit=API_ANSWER_LIMIT, body=data, headers=headers)
    except urllib.error.HTTPError as error:
        code, answered = error.code, error.read(API_ANSWER_LIMIT)
    try:
        answer = json.loads(answered)
    except ValueError:
        answer = {}
    return code, answer if isinstance(answer, dict) else {}


def callTelegram(token, method, payload=None):
    code, answer = callApi(f"{TELEGRAM_API}/bot{token.strip()}/{method}", payload)
    if not answer.get("ok"):
        raise MessagingError(describeTelegramError(code, str(answer.get("description", ""))))
    return answer.get("result")


def callWhatsapp(settings, path, payload=None):
    code, answer = callApi(f"{WHATSAPP_API}/{path}", payload, {"Authorization": f"Bearer {settings['token'].strip()}"})
    if code >= 400 or "error" in answer:
        raise MessagingError(describeWhatsappError(answer.get("error") or {}, code))
    return answer


# Telegram and WhatsApp refuse a message above 4096 characters, so a long text is sent in parts, cut at the end of a line when possible.
def splitMessage(text, limit=MESSAGE_LIMIT):
    parts, rest = [], text.strip()
    while len(rest) > limit:
        cut = limit
        for mark in ("\n", " "):
            found = rest.rfind(mark, 0, limit)
            if found > limit // 2:
                cut = found
                break
        parts.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    return parts + [rest] if rest else parts


def sendTelegram(settings, text):
    for part in splitMessage(text):
        callTelegram(settings["token"], "sendMessage", {"chat_id": settings["chat"].strip(), "text": part})


def sendWhatsapp(settings, text):
    for part in splitMessage(text):
        callWhatsapp(settings, f"{settings['phoneId'].strip()}/messages", {"messaging_product": "whatsapp", "recipient_type": "individual", "to": settings["to"].strip(),
                                                                              "type": "text", "text": {"preview_url": False, "body": part}})


def checkTelegram(settings):
    callTelegram(settings["token"], "getMe")
    callTelegram(settings["token"], "getChat", {"chat_id": settings["chat"].strip()})


# The receiver of a WhatsApp message cannot be checked without sending to it, so only the token and the phone number ID are.
def checkWhatsapp(settings):
    callWhatsapp(settings, f"{settings['phoneId'].strip()}?fields=display_phone_number")


MESSENGERS = {"Telegram": sendTelegram, "WhatsApp": sendWhatsapp}
MESSENGER_CHECKS = {"Telegram": checkTelegram, "WhatsApp": checkWhatsapp}


# Runs a call to a messaging app. A lost internet connection is a ConnectionLost, and any other failure to reach the app is a MessagingError.
def callMessenger(app, action):
    try:
        return action()
    except OSError as error:
        raiseIfOffline(error)
        raise MessagingError(f"{app} took too long to answer." if isinstance(error, TimeoutError) else f"{app} could not be reached.") from error


def sendMessage(app, settings, text):
    if app not in MESSENGERS:
        raise MessagingError(f"{app} is not one of the messaging apps: {', '.join(MESSENGERS)}.")
    if not text.strip():
        raise MessagingError("There is nothing to send.")
    callMessenger(app, lambda: MESSENGERS[app](settings, text))


# Tries the connection and the information without sending anything. It returns "", or a sentence for the user.
def checkMessenger(app, settings):
    if app not in MESSENGER_CHECKS:
        return f"{app} is not one of the messaging apps: {', '.join(MESSENGERS)}."
    try:
        callMessenger(app, lambda: MESSENGER_CHECKS[app](settings))
    except (MessagingError, ConnectionLost) as error:
        return str(error)
    return ""


# The chats that wrote to a Telegram bot lately, newest first, for a user who does not know their chat number.
def findTelegramChats(token):
    updates = callMessenger("Telegram", lambda: callTelegram(token, "getUpdates")) or []
    chats = {}
    for update in reversed(updates):
        message = next((update[kind] for kind in ("message", "edited_message", "channel_post") if kind in update), {})
        chat = message.get("chat") or {}
        if "id" in chat:
            name = chat.get("title") or " ".join(part for part in (chat.get("first_name"), chat.get("last_name")) if part) or chat.get("username") or str(chat["id"])
            chats.setdefault(chat["id"], name)
    return [{"id": chatId, "name": name} for chatId, name in chats.items()]
