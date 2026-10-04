import getpass
import html
import http.client
import imaplib
import json
import os
import re
import shutil
import smtplib
import socket
import subprocess
import sys
import threading
import traceback
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ElementTree
import zlib
from collections import Counter
from datetime import datetime, timedelta, timezone
from email import message_from_bytes, policy
from email.message import EmailMessage
from functools import partial
from pathlib import Path

import agent_prompts as prompts
from models_library import PRICE_NOTES, PRICING_PAGES
from sources_library import ALL_NEWS_OUTLETS

# We need to define a set of tools used in each harness module.
# A harness module is then a list of functions/classes that
# are necessary for a given task.
# An agent is any object with an input(prompt) method that returns the model's answer as text.
# Every loop writes a draft, verifies it, asks the user, and only then acts (send, book, save...).
# The major prompts of the loops are in agent_prompts.py and the lists to pick from are in sources_library.py.

PROJECT_FOLDER = Path(__file__).resolve().parent.parent
AGENT_FILES = PROJECT_FOLDER / "agent-files"
AGENT_RULES = PROJECT_FOLDER / "agent-rules"
MAX_CONTEXT_ITEMS = 5
INBOX_SEARCH_LIMIT = 10
PAPERS_PER_SEARCH = 10
SUMMARY_LENGTH = 1500
MIN_ABSTRACT_LENGTH = 400
FETCH_TIMEOUT = 20
MAX_DOWNLOAD = 5000000
RUN_TIMEOUT = 60
USER_AGENT = "Mozilla/5.0 (compatible; HarnessForGood/1.0)"
YES_WORDS = ("yes", "y", "ok", "okay", "approve")
NO_WORDS = ("no", "n", "cancel", "stop")
USER_NAME = "User"
PLAN_LENGTH = 150
CORRECTION_REPLY = "The user asked for a correction. It is in your newest messages, apply it."
EMAIL_PATTERN = r"[^@\s]+@[^@\s]+\.[^@\s]+"
PHONE_PATTERN = r"\+\d{6,15}"
DEFAULT_NEWS_OUTLETS = ("BBC World", "NPR News")
ABSTRACT_META_NAMES = ("citation_abstract", "dc.description", "og:description", "description")
MODEL_PRICES_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
PRICE_PROVIDERS = {"gpt": "openai", "claude": "anthropic", "gemini": "gemini", "deepseek": "deepseek"}
PRICE_REFRESH_SECONDS = 3600
PRICE_FILE_LIMIT = 20000000
GPU_TIMEOUT = 10
GPU_REFRESH_SECONDS = 5
MEBIBYTE = 1048576
DRM_FOLDER = Path("/sys/class/drm")
NO_GPU_MESSAGE = "No supported GPU was found (NVIDIA on Linux and Windows, AMD on Linux), so local models cannot run. Choose API models."
FETCH_ERRORS = (OSError, ValueError, ElementTree.ParseError, http.client.HTTPException, zlib.error)
# Public servers that are asked for a connection (nothing is sent) to know if the internet works at all.
CONNECTION_HOSTS = (("1.1.1.1", 443), ("8.8.8.8", 443), ("9.9.9.9", 443))
CONNECTION_TIMEOUT = 3
TELEGRAM_API = "https://api.telegram.org"
WHATSAPP_API = "https://graph.facebook.com/v25.0"
API_ANSWER_LIMIT = 100000
MESSAGE_LIMIT = 4000
# The state of a running swarm is saved in RUNS_FOLDER (inside agent-files) at every change, and also every HEARTBEAT_SECONDS.
# A saved swarm that has not been written for STALE_SECONDS is not running anymore. STOP_TIMEOUT is how long a stopped swarm waits for its agents.
RUNS_FOLDER = "swarm-runs"
STATE_VERSION = 1
HEARTBEAT_SECONDS = 5
STALE_SECONDS = 30
STOP_TIMEOUT = 10
STOPPED_MESSAGE = "The user stopped the swarm before this agent finished."
# What is saved of an agent of a swarm, apart from its loop and the lists.
SAVED_FIELDS = ("role", "task", "boss", "model", "recipe", "status", "result", "error", "mode", "review", "draft", "problem", "revision", "started")
# The agents of a swarm work at the same time, so they take turns to write the files and to speak to the user.
CONTEXT_LOCK = threading.RLock()
USER_LOCK = threading.RLock()
STATE_LOCK = threading.Lock()


# ==============
# Shared tools. Defined once and reused by every loop.
# ==============
def loadContext(fileName):
    path = AGENT_FILES / fileName
    with CONTEXT_LOCK:
        if not path.exists() or path.stat().st_size == 0:
            return {}
        return json.loads(path.read_text(encoding="utf-8"))


def saveContext(fileName, context):
    with CONTEXT_LOCK:
        (AGENT_FILES / fileName).write_text(json.dumps(context, indent=4, ensure_ascii=False), encoding="utf-8")


def getFromContext(fileName, keys):
    value = loadContext(fileName)
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return value


def addToContext(fileName, keys, value):
    with CONTEXT_LOCK:
        context = loadContext(fileName)
        place = context
        for key in keys[:-1]:
            place = place.setdefault(key, {})
        place[keys[-1]] = value
        saveContext(fileName, context)


def retrieveContext(fileName, keys):
    items = getFromContext(fileName, keys) or {}
    recent = dict(list(items.items())[-MAX_CONTEXT_ITEMS:])
    return json.dumps(recent, indent=2, ensure_ascii=False) if recent else "Nothing yet."


def loadRules(fileName):
    if not fileName or not (AGENT_RULES / fileName).exists():
        return ""
    return (AGENT_RULES / fileName).read_text(encoding="utf-8").strip()


def isYes(reply):
    return reply.strip().lower() in YES_WORDS


def isNo(reply):
    answer = reply.strip().lower()
    return answer in NO_WORDS or answer == ""


def stripFences(text):
    match = re.search(r"```[\w+-]*\n(.*?)```", text, re.DOTALL)
    return match.group(1).strip() if match else text.strip()


def getWords(text):
    return re.findall(r"\w+", text.lower())


def checkLength(text, maxWords):
    count = len(text.split())
    return f"The text has {count} words but the limit is {maxWords}. Shorten it." if count > maxWords else ""


def stripTags(text):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text)).split())


def cleanText(element):
    return stripTags("".join(element.itertext()))


# The only place where the internet is reached. The address must be a web page, never a local file.
# An account (username, password) is only sent to https pages, and only when the page asks for it.
# With a body (bytes) the request is a POST, and headers are added to the usual ones.
def fetchUrl(url, login=None, limit=MAX_DOWNLOAD, body=None, headers=None):
    url = url.strip()
    parts = urllib.parse.urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"'{url}' is not a web address starting with http:// or https://")
    handlers = []
    if login:
        if parts.scheme != "https" and parts.hostname not in ("localhost", "127.0.0.1"):
            raise ValueError("An account is only used on secure https pages.")
        passwords = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        passwords.add_password(None, f"{parts.scheme}://{parts.netloc}/", login[0], login[1])
        handlers.append(urllib.request.HTTPBasicAuthHandler(passwords))
    request = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.build_opener(*handlers).open(request, timeout=FETCH_TIMEOUT) as response:
        data = response.read(limit)
    # Some websites send compressed data even when it was not asked for.
    return zlib.decompressobj(31).decompress(data, limit) if data[:2] == b"\x1f\x8b" else data


# Turns the errors of fetchUrl and readFeed into a sentence a user can understand.
def describeError(error):
    if isinstance(error, urllib.error.HTTPError):
        if error.code == 429:
            return "the website says it received too many requests, try again later"
        return f"the website answered with error {error.code}"
    if isinstance(error, TimeoutError):
        return "the website took too long to answer"
    if isinstance(error, ElementTree.ParseError):
        return "this address is not an RSS or Atom feed"
    if isinstance(error, OSError):
        return "the website could not be reached"
    return str(error)


# The connection that an agent needs is gone. Inside a swarm the agent is paused (not failed) and the user chooses to continue or to stop.
class ConnectionLost(Exception):
    pass


# Raised in the agents that were waiting when the user stopped the swarm.
class SwarmStopped(Exception):
    pass


# The places to ask for a connection: the proxy of the user if there is one (it is how this computer reaches the internet), then public servers.
def connectionTargets():
    proxies = [urllib.parse.urlparse(address) for name, address in urllib.request.getproxies().items() if name != "no"]
    return [(proxy.hostname, proxy.port or (443 if proxy.scheme == "https" else 80)) for proxy in proxies if proxy.hostname] + list(CONNECTION_HOSTS)


# Asks for a connection, without sending anything. It tells "my internet is down" from "this one website is down".
def isOnline():
    for target in connectionTargets():
        try:
            socket.create_connection(target, timeout=CONNECTION_TIMEOUT).close()
            return True
        except OSError:
            continue
    return False


# The ConnectionLost that an error stands for, or None. A network error (OSError) is a lost connection only if the internet is really gone:
# an answer of the server (an HTTP or SMTP error) proves that the connection works.
def asConnectionLost(error):
    if isinstance(error, ConnectionLost):
        return error
    answered = isinstance(error, (urllib.error.HTTPError, smtplib.SMTPResponseException))
    if isinstance(error, OSError) and not answered and not isOnline():
        return ConnectionLost("The internet connection was lost.")
    return None


# Call it in the except block of a network step that must not fail when the internet is gone.
def raiseIfOffline(error):
    lost = asConnectionLost(error)
    if lost:
        raise lost from error


def findFeedLink(page, url):
    for tag in re.findall(r"<link\s[^>]*>", page, re.IGNORECASE):
        href = re.search(r'href=["\']([^"\']+)["\']', tag, re.IGNORECASE)
        if href and re.search(r"application/(?:rss|atom)\+xml", tag, re.IGNORECASE):
            return urllib.parse.urljoin(url, html.unescape(href.group(1)))
    return ""


def readFeed(url, limit=10, discover=True):
    data = fetchUrl(url)
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError:
        # The address may be the web page of the outlet instead of its feed. Look for the feed in it.
        feedUrl = findFeedLink(data.decode("utf-8", errors="replace"), url) if discover else ""
        if not feedUrl:
            raise
        return readFeed(feedUrl, limit, discover=False)
    # RSS feeds use <item> and Atom feeds (like arXiv) use <entry>.
    entries = [entry for entry in root.iter() if entry.tag.split("}")[-1] in ("item", "entry")]
    items = []
    for entry in entries[:limit]:
        fields = {}
        for child in entry:
            fields.setdefault(child.tag.split("}")[-1], child.get("href") or cleanText(child))
        summary = fields.get("description") or fields.get("summary") or fields.get("content") or ""
        items.append({"title": fields.get("title", ""), "summary": summary[:SUMMARY_LENGTH], "link": fields.get("link", "")})
    return items


def formatItems(items):
    return "\n".join(f"- {item['title']}: {item['summary']} ({item['link']})" for item in items)


# Publishers put the abstract of a paper in different tags of the page.
# The longest one is the abstract, the others are short descriptions of the website.
def readAbstract(page):
    if page[:4] == b"%PDF":
        return ""
    meta = {}
    for tag in re.findall(r"<meta\s[^>]*>", page.decode("utf-8", errors="replace"), re.IGNORECASE):
        name = re.search(r'(?:name|property)=["\']([^"\']+)["\']', tag, re.IGNORECASE)
        content = re.search(r'content=(?:"([^"]*)"|\'([^\']*)\')', tag, re.IGNORECASE)
        if name and content:
            # Some publishers encode the special characters of their own tags twice, so they are decoded one more time.
            meta.setdefault(name.group(1).lower(), html.unescape(stripTags(content.group(1) or content.group(2) or "")))
    return max([meta.get(name, "") for name in ABSTRACT_META_NAMES], key=len)


modelPriceCache = {"loaded": 0, "prices": {}}


def perMillion(cost):
    return round(cost * 1000000, 4) if cost is not None else None


# The prices of all the API models, in US dollars per 1 million tokens, from a list kept up to date by the community (LiteLLM).
# For the models checked it agrees with the pages of the providers. The list is downloaded again after an hour,
# and if that fails the last one is kept, so the info buttons keep working without internet for a while.
def loadModelPrices():
    now = datetime.now().timestamp()
    if now - modelPriceCache["loaded"] > PRICE_REFRESH_SECONDS:
        try:
            entries = json.loads(fetchUrl(MODEL_PRICES_URL, limit=PRICE_FILE_LIMIT))
            if not isinstance(entries, dict):
                raise ValueError("the price list has an unexpected format")
        except FETCH_ERRORS:
            if not modelPriceCache["prices"]:
                raise
            return modelPriceCache["prices"]
        prices = {}
        for name, entry in entries.items():
            if isinstance(entry, dict) and all(isinstance(entry.get(field), (int, float)) for field in ("input_cost_per_token", "output_cost_per_token")):
                prices[name] = {"input": perMillion(entry["input_cost_per_token"]), "output": perMillion(entry["output_cost_per_token"]),
                                "cachedInput": perMillion(entry.get("cache_read_input_token_cost")), "context": entry.get("max_input_tokens")}
        modelPriceCache.update(loaded=now, prices=prices)
    return modelPriceCache["prices"]


# What the info button of a model shows. It never fails: if the price is not known, it says why and still gives the official page.
def getModelCost(provider, model):
    page = PRICING_PAGES.get(provider, "")
    try:
        prices = loadModelPrices()
    except FETCH_ERRORS as error:
        return {"model": model, "error": f"The price could not be fetched: {describeError(error)}.", "page": page}
    price = prices.get(model) or prices.get(f"{PRICE_PROVIDERS.get(provider, provider)}/{model}")
    if not price:
        return {"model": model, "error": "No price is published for this model yet.", "page": page}
    return {"model": model, **price, "unit": "US dollars per 1 million tokens", "note": PRICE_NOTES.get(provider, ""), "page": page}


gpuCache = {"loaded": 0, "gpus": []}


def bytesToGb(size):
    return round(size / 1000000000, 1)


# nvidia-smi comes with the NVIDIA driver, on Linux and on Windows. On Windows it is not always in the PATH.
def findNvidiaSmi():
    found = shutil.which("nvidia-smi")
    if found:
        return found
    legacy = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe"
    return str(legacy) if legacy.exists() else None


# The memory of NVIDIA GPUs, in GB. It never fails: with no tool, no driver, or an answer it cannot read, there is no GPU.
# CREATE_NO_WINDOW (it exists on Windows only) keeps a window from flashing when a user interface asks.
def readNvidiaGpus():
    command = findNvidiaSmi()
    if not command:
        return []
    try:
        result = subprocess.run([command, "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader,nounits"], capture_output=True, text=True,
                                timeout=GPU_TIMEOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return []
    gpus = []
    for line in result.stdout.splitlines() if result.returncode == 0 else []:
        try:
            *name, total, free = [part.strip() for part in line.split(",")]
            gpus.append({"name": ", ".join(name), "total": bytesToGb(float(total) * MEBIBYTE), "free": bytesToGb(float(free) * MEBIBYTE)})
        except ValueError:
            continue
    return gpus


# The memory of AMD GPUs on Linux. The amdgpu driver writes it in files, so no tool is needed.
# Only card0, card1... are read, because the folders of the screen connectors (card0-DP-1...) link to the same device.
def readAmdGpus():
    try:
        cards = sorted(DRM_FOLDER.iterdir())
    except OSError:
        return []
    gpus = []
    for card in cards:
        if not re.fullmatch(r"card\d+", card.name):
            continue
        try:
            total, used = (int((card / "device" / f"mem_info_vram_{kind}").read_text()) for kind in ("total", "used"))
        except (OSError, ValueError):
            continue
        try:
            name = (card / "device" / "product_name").read_text().strip()
        except OSError:
            name = ""
        gpus.append({"name": name or f"AMD GPU {card.name}", "total": bytesToGb(total), "free": bytesToGb(total - used)})
    return gpus


def queryGpus():
    return readNvidiaGpus() + readAmdGpus()


# The GPUs are only read again after a few seconds, so a list of many models can be checked without starting the tool for each one.
def readGpus():
    now = datetime.now().timestamp()
    if now - gpuCache["loaded"] > GPU_REFRESH_SECONDS:
        gpuCache.update(loaded=now, gpus=queryGpus())
    return gpuCache["gpus"]


# What the GPUs can host, in GB. total is all their memory, and free is what no other job uses at this moment, which keeps changing.
# fits says the swarm could run on these GPUs if nothing else used them, and runnable says it can run right now.
def checkVram(needed, gpus):
    total, free = round(sum(gpu["total"] for gpu in gpus), 1), round(sum(gpu["free"] for gpu in gpus), 1)
    used = round(total - free, 1)
    message = ""
    if needed and not gpus:
        message = NO_GPU_MESSAGE
    elif needed > total:
        message = f"The swarm needs about {needed} GB of VRAM but your GPUs only have {total} GB in total."
    elif needed > free:
        message = (f"The swarm needs about {needed} GB of VRAM. Your GPUs have {total} GB in total, but only {free} GB are free now "
                   f"because other jobs use {used} GB. You will not be able to run the swarm until they free enough memory.")
    return {"gpus": gpus, "needed": needed, "total": total, "free": free, "used": used, "left": round(total - needed, 1),
            "fits": needed <= total, "runnable": needed <= free, "message": message}


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


# The parent of all loops. A loop only has to define its tools, its task, and how to verify the draft.
class Loop:
    def __init__(self, agent, numberOfLoops=5, rulesFile=None):
        self.agent = agent
        self.numberOfLoops = numberOfLoops
        self.rules = loadRules(rulesFile)
        self.settings = {}
        self.inbox = []
        self.userMessages = []
        self.approvedPlan = ""
        self.planning = False
        self.reviewer = None
        self.name = type(self).__name__
        self.folder = None
        # What the agent did in its run, so a run that was interrupted goes on from there instead of starting again (see Swarm.resume).
        # progress is only read when resumed is True. A swarm sets onProgress, onConnectionLost and resumed.
        self.progress = {}
        self.actions = []
        self.resumed = False
        self.onProgress = None
        self.onConnectionLost = None

    # Messages sent by the other agents of a swarm. The agent reads them with every prompt.
    def receive(self, sender, message):
        self.inbox.append(f"{sender}: {message}")

    # Messages typed by the user while the agent works. The agent reads them with its next prompt,
    # and reviewLoop writes again a draft that was started before one of them arrived.
    def receiveFromUser(self, message):
        self.userMessages.append(message)

    # Everything an agent needs to go on after the program stopped. It holds no password: those are never saved.
    def getState(self):
        return {"progress": dict(self.progress), "actions": list(self.actions), "inbox": list(self.inbox), "userMessages": list(self.userMessages),
                "approvedPlan": self.approvedPlan}

    def setState(self, state):
        self.progress = dict(state.get("progress", {}))
        self.actions = list(state.get("actions", []))
        self.inbox = list(state.get("inbox", []))
        self.userMessages = list(state.get("userMessages", []))
        self.approvedPlan = state.get("approvedPlan", "")

    def saveProgress(self):
        if self.onProgress:
            self.onProgress()

    def remember(self, key, value):
        self.progress[key] = value
        self.saveProgress()

    # What the agent changed outside of itself (an email sent, a file written...). The leader tells it to the user if the swarm is stopped.
    def logAction(self, text):
        self.actions.append({"time": f"{datetime.now():%Y-%m-%d %H:%M:%S}", "text": text})
        self.saveProgress()

    # The files the agent works on by itself. They must be inside its folder.
    def files(self):
        return []

    # The folder where the agent works: what it saves goes in it, and the files it works on must be in it. Without a folder nothing changes.
    def setFolder(self, folder):
        if not folder:
            self.folder = None
            return
        path = Path(folder).expanduser().resolve()
        if not path.is_dir():
            raise ValueError(f"There is no folder at {path}.")
        for file in self.files():
            if not file.expanduser().resolve().is_relative_to(path):
                raise ValueError(f"{file.name} is not inside {path}. Choose a folder that contains it.")
        self.folder = path

    def describeFolder(self):
        return f" It works inside the folder {self.folder}." if self.folder else ""

    # Saves what the agent produced as a file in its folder, if it has one. The name has the time of the run, and a run that is
    # resumed keeps it, so the same file is written again instead of a new one.
    def saveResult(self, name, text, suffix=".md"):
        if not self.folder:
            return None
        if not (self.resumed and self.progress.get("stamp")):
            self.progress["stamp"] = f"{datetime.now():%Y-%m-%d_%H-%M-%S}"
        path = self.folder / f"{name}_{self.progress['stamp']}{suffix}"
        path.write_text(text, encoding="utf-8")
        self.logAction(f"Saved {path}.")
        return path

    # Puts back what an unfinished run changed in the files the user already had. Only the agents that do that need it.
    def rollback(self):
        pass

    # What the agent would leave half done if the swarm were stopped now.
    def describePending(self):
        return ""

    # Runs a step that needs the network. If the connection is lost inside a swarm, the agent waits here until the user decides:
    # to continue (the step is tried again) or to stop (SwarmStopped). Outside of a swarm the error goes up as it is.
    def keepTrying(self, step):
        while True:
            try:
                return step()
            except (ConnectionLost, OSError) as error:
                lost = asConnectionLost(error) if self.onConnectionLost else None
                if not lost:
                    raise
                self.onConnectionLost(lost)

    # Inside a swarm, a step that found nothing because the internet is gone is a lost connection, and not an empty result.
    def checkOnline(self, what):
        if self.onConnectionLost and not isOnline():
            raise ConnectionLost(f"The internet connection was lost while {what}.")

    # An email or a message cannot be taken back, so it must never go twice. If the program stopped while it was being sent, nobody knows
    # if it left, so the user decides. It returns True if it was sent now.
    def sendOnce(self, key, send, what):
        state = self.progress.get(key) if self.resumed else None
        if state == "sent":
            return False
        if state == "sending" and not isYes(self.askUser(f"The program stopped while {what}. It may or may not have gone out. Send it again? (yes/no)")):
            self.remember(key, "sent")
            return False
        self.remember(key, "sending")
        try:
            self.keepTrying(send)
        except BaseException:
            self.remember(key, "")
            raise
        self.remember(key, "sent")
        return True

    # own is False for the calls a leader makes to manage the swarm (summaries, corrections): the rules and the plan
    # of the task of the leader, like the rules of emails, do not apply to them.
    def askAgent(self, prompt, own=True):
        if own and self.approvedPlan:
            prompt = prompts.APPROVED_PLAN_PROMPT.format(plan=self.approvedPlan) + f"\n\n{prompt}"
        if self.userMessages:
            prompt = prompts.USER_MESSAGES_PROMPT.format(messages="\n".join(f"- {message}" for message in self.userMessages)) + f"\n\n{prompt}"
        if self.inbox:
            prompt = "Messages from the other agents of your swarm:\n" + "\n".join(self.inbox) + f"\n\n{prompt}"
        if own and self.rules:
            prompt = f"Follow these rules strictly:\n{self.rules}\n\n{prompt}"
        # The calls of a leader that manages the swarm never wait for the user: they have a fallback of their own.
        return self.keepTrying(lambda: self.agent.input(prompt)) if own else self.agent.input(prompt)

    # The next three are the only places where the user is spoken to. A user interface can replace them.
    def notifyUser(self, message):
        with USER_LOCK:
            print(message)

    def askUser(self, question):
        with USER_LOCK:
            return input(f"{question} ")

    def askSecret(self, question):
        with USER_LOCK:
            return getpass.getpass(f"{question} ")

    # Passwords and addresses of servers come from the environment variables, otherwise the user is asked once.
    # They are only kept in memory while the loop exists, never saved to a file.
    def getSetting(self, name, secret=False):
        if name not in self.settings:
            question = f"Please enter {name}:"
            self.settings[name] = os.environ.get(name) or (self.askSecret(question) if secret else self.askUser(question))
        return self.settings[name]

    # Returns (username, password) of an account the user has on a website, or None if the user has none.
    def askLogin(self, host):
        with USER_LOCK:
            self.notifyUser(f"[{self.name}] {host} asks for an account. It is only used for this run and never saved. Leave the username empty to skip.")
            username = self.askUser(f"Username for {host}:").strip()
            return (username, self.askSecret(f"Password for {host}:")) if username else None

    def describe(self, draft):
        return draft

    # What the agent does, in a sentence. The swarm gives it to the agent as its task, and the user reads it.
    def describeTask(self):
        return ""

    # When the agent must start, as a datetime, if it has a time of its own. None means as soon as the agents it waits for are done.
    def startTime(self):
        return None

    def checkRules(self, draft):
        if not self.rules:
            return ""
        answer = self.askAgent("Check the draft below against the rules that apply to what the agent writes. "
                               f"Reply only OK if it follows all of them, otherwise list the rules it breaks.\n\n{draft}")
        return "" if answer.strip().upper().startswith("OK") else answer

    def checkNewMessages(self, messagesBefore):
        return "The user sent new messages while this draft was being written. Make sure the draft follows them." if len(self.userMessages) > messagesBefore else ""

    def checkPlanText(self, draft):
        return checkLength(draft, PLAN_LENGTH) if draft.strip() else "Write the plan."

    # Where the user decides about a draft. The answer is yes, no, or what to change.
    # A swarm sets reviewer, so the user can answer through the summary of the leader or by clicking on the agent, instead of in the console.
    def askApproval(self, draft, problem):
        shown = draft if self.planning else self.describe(draft)
        if self.reviewer:
            return self.reviewer(shown, problem)
        with USER_LOCK:
            self.notifyUser(f"[{self.name}]\n{shown}")
            self.notifyUser(f"Warning, the automatic checks found a problem: {problem}" if problem else "The automatic checks passed.")
            return self.askUser("Is this good to go? Type yes, no, or what you want changed:")

    # Planning mode of an agent: it writes a plan for its task and does nothing else. The plan needs the approval of the user.
    # Once approved, the plan is part of every prompt of the agent, so it follows it when it executes.
    def makePlan(self, task):
        self.planning = True
        try:
            plan = self.reviewLoop(prompts.PLAN_PROMPT.format(task=task, maxWords=PLAN_LENGTH), self.checkPlanText)
        finally:
            self.planning = False
        self.approvedPlan = plan or ""
        return plan

    # Write a draft, verify it, show it to the user, and improve it until the user approves.
    # verify(draft) returns "" if the draft is fine, or a sentence describing the problem.
    # Without ask the user is not involved: the draft is returned when it passes the checks, otherwise None.
    # With a key the approved draft is remembered, and a run that is resumed gets it back instead of asking the user again.
    def reviewLoop(self, task, verify, ask=True, own=True, key=None):
        if key and self.resumed and key in self.progress:
            return self.progress[key]
        draft, feedback = "", ""
        for attempt in range(1, self.numberOfLoops + 1):
            prompt = task
            if feedback:
                prompt = f"{task}\n\nYour previous draft:\n{draft}\n\nImprove it. What to change: {feedback}"
            messagesBefore = len(self.userMessages)
            draft = self.askAgent(prompt, own)
            problem = verify(draft) or self.checkNewMessages(messagesBefore)
            if problem and attempt < self.numberOfLoops:
                feedback = problem
                continue
            if not ask:
                return None if problem else draft
            reply = self.askApproval(draft, problem)
            if isYes(reply):
                if key:
                    self.remember(key, draft)
                return draft
            if isNo(reply):
                return None
            feedback = reply
        self.notifyUser(f"Stopped after {self.numberOfLoops} drafts.")
        return None


# ==============
# Email harness.
# Needs EMAIL_PASSWORD, EMAIL_SMTP_SERVER and (to read replies) EMAIL_IMAP_SERVER, from the settings of the loop or from the environment variables.
# If the first two are not set, the user is asked for them.
# ==============
# Tries the logins without sending anything, so a wrong password is found before the swarm runs. It returns "", or a sentence for the user.
def checkEmailLogin(sender, password, smtpServer, imapServer=""):
    try:
        with smtplib.SMTP(smtpServer, 587, timeout=FETCH_TIMEOUT) as server:
            server.starttls()
            server.login(sender, password)
    except smtplib.SMTPAuthenticationError:
        return f"{smtpServer} refused the address or the password. Providers like Gmail, Outlook and Yahoo need an app password, not the normal one."
    except (OSError, smtplib.SMTPException) as error:
        return f"Could not send through {smtpServer}: {error}"
    if imapServer:
        try:
            with imaplib.IMAP4_SSL(imapServer, timeout=FETCH_TIMEOUT) as inbox:
                inbox.login(sender, password)
        except (OSError, imaplib.IMAP4.error) as error:
            return f"Sending works, but {imapServer} could not be used to read the replies: {error}"
    return ""


class EmailLoop(Loop):
    def __init__(self, agent, sender, receiver, subject, request, language="English", numberOfLoops=5):
        super().__init__(agent, numberOfLoops, "EMAIL_RULES.md")
        self.sender = sender
        self.receiver = receiver
        self.subject = subject
        self.request = request
        self.language = language

    def fetchLatestEmail(self):
        server = self.settings.get("EMAIL_IMAP_SERVER") or os.environ.get("EMAIL_IMAP_SERVER")
        if not server:
            self.notifyUser("EMAIL_IMAP_SERVER is not set, so your inbox is not read. Writing a new email.")
            return ""
        with imaplib.IMAP4_SSL(server) as inbox:
            inbox.login(self.sender, self.getSetting("EMAIL_PASSWORD", secret=True))
            inbox.select("INBOX", readonly=True)
            status, found = inbox.search(None, "FROM", f'"{self.receiver}"')
            for emailId in reversed(found[0].split()[-INBOX_SEARCH_LIMIT:]):
                status, data = inbox.fetch(emailId, "(RFC822)")
                message = message_from_bytes(data[0][1], policy=policy.default)
                if self.subject.lower() in str(message["subject"]).lower():
                    body = message.get_body(preferencelist=("plain", "html"))
                    return body.get_content() if body else ""
        return ""

    def describe(self, draft):
        return f"To: {self.receiver}\nSubject: {self.subject}\n\n{draft}"

    def describeTask(self):
        return f"Write an email in {self.language} to {self.receiver} with the subject \"{self.subject}\" saying: {self.request}. It is sent from {self.sender} once the user approves it."

    def sendEmail(self, mail):
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = self.receiver
        message["Subject"] = self.subject
        message.set_content(mail)
        with smtplib.SMTP(self.getSetting("EMAIL_SMTP_SERVER"), 587) as server:
            server.starttls()
            server.login(self.sender, self.getSetting("EMAIL_PASSWORD", secret=True))
            server.send_message(message)

    def run(self):
        if not re.fullmatch(EMAIL_PATTERN, self.receiver):
            self.notifyUser(f"{self.receiver} is not a valid email address.")
            return None
        task = prompts.EMAIL_PROMPT.format(language=self.language, sender=self.sender, receiver=self.receiver, subject=self.subject,
                                           request=self.request, incoming=self.keepTrying(self.fetchLatestEmail) or "None, this is a new email.",
                                           sent=retrieveContext("email_contexts.json", [self.sender, self.receiver]))
        mail = self.reviewLoop(task, self.checkRules, key="mail")
        if mail is None:
            self.notifyUser("The email was not sent.")
            return None
        if self.sendOnce("sent", lambda: self.sendEmail(mail), "sending the email"):
            self.logAction(f"Sent the email \"{self.subject}\" to {self.receiver}.")
        addToContext("email_contexts.json", [self.sender, self.receiver, self.subject], mail)
        self.saveResult("sent_email", self.describe(mail), ".txt")
        self.notifyUser(f"Email sent to {self.receiver}.")
        return mail


# ==============
# Calendar harness.
# The events are kept in calendar_contexts.json and exported to calendar_events.ics,
# which can be imported into Google Calendar, Outlook, Apple Calendar, etc.
# ==============
class CalendarLoop(Loop):
    def __init__(self, agent, request, numberOfLoops=5):
        super().__init__(agent, numberOfLoops, "CALENDAR_RULES.md")
        self.request = request

    def describeTask(self):
        return f"Book this event in the calendar: {self.request}"

    def parseEvent(self, draft):
        event = json.loads(stripFences(draft))
        start = datetime.strptime(f"{event['date']} {event['time']}", "%Y-%m-%d %H:%M")
        return start, int(event["duration"]), str(event["subject"])

    def upcomingEvents(self):
        today = f"{datetime.now():%Y-%m-%d}"
        upcoming = {date: events for date, events in sorted(loadContext("calendar_contexts.json").items()) if date >= today}
        return json.dumps(upcoming, ensure_ascii=False) if upcoming else "None, the calendar is free."

    def checkEvent(self, draft):
        try:
            start, duration, subject = self.parseEvent(draft)
        except (ValueError, KeyError, TypeError):
            return f"Reply only with JSON like {prompts.EVENT_EXAMPLE}"
        if start < datetime.now() or duration <= 0:
            return "The event must start in the future and have a duration above 0 minutes."
        return ""

    def findOverlap(self, start, duration):
        end = start + timedelta(minutes=duration)
        for time, event in (getFromContext("calendar_contexts.json", [f"{start:%Y-%m-%d}"]) or {}).items():
            otherStart = datetime.strptime(f"{start:%Y-%m-%d} {time}", "%Y-%m-%d %H:%M")
            if start < otherStart + timedelta(minutes=event["duration"]) and otherStart < end:
                return f"'{event['subject']}' at {time} for {event['duration']} minutes"
        return ""

    # An overlap is not a mistake of the agent, so the user decides: book it anyway or choose another time.
    def describe(self, draft):
        try:
            start, duration, subject = self.parseEvent(draft)
        except (ValueError, KeyError, TypeError):
            return draft
        overlap = self.findOverlap(start, duration)
        warning = f"\nWarning, this overlaps with {overlap}. Type yes to book it anyway, or tell me another time." if overlap else ""
        return f"{subject}\n{start:%A %d %B %Y} at {start:%H:%M} for {duration} minutes{warning}"

    def exportCalendar(self):
        stamp = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//SwarmUP//Calendar//EN"]
        for date, events in loadContext("calendar_contexts.json").items():
            for time, event in events.items():
                start = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
                end = start + timedelta(minutes=event["duration"])
                # The .ics format needs backslashes, semicolons and commas to be escaped.
                subject = re.sub(r"([\\;,])", r"\\\1", event["subject"]).replace("\n", "\\n")
                lines += ["BEGIN:VEVENT", f"UID:{start:%Y%m%dT%H%M}@swarm-up", f"DTSTAMP:{stamp}",
                          f"DTSTART:{start:%Y%m%dT%H%M%S}", f"DTEND:{end:%Y%m%dT%H%M%S}", f"SUMMARY:{subject}", "END:VEVENT"]
        lines.append("END:VCALENDAR")
        for place in (AGENT_FILES, self.folder):
            if place:
                (place / "calendar_events.ics").write_text("\r\n".join(lines) + "\r\n", encoding="utf-8", newline="")

    def run(self):
        task = prompts.CALENDAR_PROMPT.format(today=f"{datetime.now():%A %Y-%m-%d %H:%M}", request=self.request,
                                              booked=self.upcomingEvents(), example=prompts.EVENT_EXAMPLE)
        event = self.reviewLoop(task, self.checkEvent, key="event")
        if event is None:
            self.notifyUser("The event was not booked.")
            return None
        start, duration, subject = self.parseEvent(event)
        addToContext("calendar_contexts.json", [f"{start:%Y-%m-%d}", f"{start:%H:%M}"], {"subject": subject, "duration": duration})
        self.exportCalendar()
        calendarFile = (self.folder or AGENT_FILES) / "calendar_events.ics"
        self.logAction(f"Booked \"{subject}\" on {start:%Y-%m-%d} at {start:%H:%M} for {duration} minutes, and wrote {calendarFile}.")
        self.notifyUser(f"Booked. Import {calendarFile} into your calendar app.")
        return self.describe(event)


# ==============
# News briefing harness.
# The outlets are names from NEWS_OUTLETS in sources_library.py (a drop list), or any web address the user writes.
# An outlet that fails is skipped with a message. Nothing is saved until the user approves the briefing.
# collectAt is the time of the day (HH:MM) when the feeds are collected. A swarm makes the agent wait for it, and without it
# the feeds are collected right away.
# ==============
# The next time the clock shows HH:MM: today if it is still to come, tomorrow otherwise.
def nextOccurrence(clock):
    try:
        hour, minute = (int(part) for part in clock.strip().split(":"))
        moment = datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
    except ValueError:
        raise ValueError(f"'{clock}' is not a time. Write it as HH:MM, like 07:30.") from None
    return moment if moment > datetime.now() else moment + timedelta(days=1)


class NewsLoop(Loop):
    # messenger is a messaging app of MESSENGERS (like Telegram) and messengerSettings what it needs (see MESSAGING_APPS in sources_library.py).
    # With them, the approved briefing is also sent to the phone of the user.
    def __init__(self, agent, topics="", outlets=DEFAULT_NEWS_OUTLETS, maxWords=250, language="English", numberOfLoops=5, collectAt=None,
                 messenger=None, messengerSettings=None):
        super().__init__(agent, numberOfLoops, "NEWS_BRIEFING_RULES.md")
        self.topics = topics
        self.outlets = outlets
        self.maxWords = maxWords
        self.language = language
        self.collectOn = collectAt if isinstance(collectAt, datetime) else nextOccurrence(collectAt) if collectAt else None
        self.messenger = messenger
        self.messengerSettings = dict(messengerSettings or {})

    def startTime(self):
        return self.collectOn

    def describeTask(self):
        schedule = f" The feeds are collected on {self.collectOn:%Y-%m-%d at %H:%M}." if self.collectOn else ""
        sending = f" The approved briefing is also sent to {self.messenger}." if self.messenger else ""
        return (f"Write a news briefing in {self.language} of at most {self.maxWords} words about {self.topics or 'anything important'}, "
                f"from the feeds of {', '.join(self.outlets)}.{schedule}{sending}")

    def fetchHeadlines(self):
        headlines, reached = [], 0
        for outlet in self.outlets:
            try:
                items = readFeed(ALL_NEWS_OUTLETS.get(outlet, outlet))
                if not items:
                    raise ValueError("it has no stories, so it is not a news feed")
            except FETCH_ERRORS as error:
                self.notifyUser(f"Skipping {outlet}: {describeError(error)}.")
                continue
            headlines += items
            reached += 1
        if not headlines:
            self.checkOnline("the news feeds were read")
        self.notifyUser(f"Read {len(headlines)} stories from {reached} of {len(self.outlets)} outlets.")
        return headlines

    # The briefing is sent only once, exactly as the user approved it.
    def deliver(self, briefing):
        send = lambda: sendMessage(self.messenger, self.messengerSettings, briefing)
        if self.messenger and self.sendOnce("delivered", send, f"sending the briefing to {self.messenger}"):
            self.logAction(f"Sent the news briefing to {self.messenger}.")
            self.notifyUser(f"The briefing was sent to {self.messenger}.")

    def run(self):
        now = datetime.now()
        date, time = f"{now:%Y-%m-%d}", f"{now:%H:%M}"
        headlines = self.keepTrying(self.fetchHeadlines)
        if not headlines:
            self.notifyUser("No headlines could be fetched, so there is no briefing.")
            return None
        task = prompts.NEWS_PROMPT.format(date=date, language=self.language, maxWords=self.maxWords, topics=self.topics or "anything important",
                                          earlier=retrieveContext("news_contexts.json", [date]), headlines=formatItems(headlines))
        briefing = self.reviewLoop(task, lambda draft: checkLength(draft, self.maxWords), key="briefing")
        if briefing is None:
            self.notifyUser("The briefing was not saved.")
            return None
        addToContext("news_contexts.json", [date, time], briefing)
        self.saveResult("news_briefing", briefing)
        self.logAction(f"Saved the news briefing of {date} at {time}.")
        self.notifyUser("The briefing is saved.")
        self.deliver(briefing)
        return briefing


# ==============
# Authoring harness.
# ==============
class AuthorLoop(Loop):
    def __init__(self, agent, subject, length, numberOfLoops=5):
        super().__init__(agent, numberOfLoops, "AUTHOR_RULES.md")
        self.subject = subject
        self.length = length

    def describeTask(self):
        return f"Write a text about {self.subject} in at most {self.length} words."

    def run(self):
        task = prompts.AUTHOR_PROMPT.format(subject=self.subject, length=self.length, earlier=retrieveContext("author_contexts.json", []))
        text = self.reviewLoop(task, lambda draft: checkLength(draft, self.length), key="text")
        if text is None:
            self.notifyUser("The text was not saved.")
            return None
        addToContext("author_contexts.json", [self.subject], text)
        self.saveResult("text", text)
        self.logAction(f"Saved the text about {self.subject}.")
        self.notifyUser("The text is saved.")
        return text


# ==============
# Literature survey harness.
# The papers are searched in several places (PAPER_SEARCHES: Google Scholar, arXiv, Crossref, OpenReview), then the page of each paper is opened
# on the website of its publisher to read the full abstract. Credentials are asked only when a publisher requires them.
# ==============
def searchGoogleScholar(subject):
    url = "https://scholar.google.com/scholar?" + urllib.parse.urlencode({"q": subject, "hl": "en"})
    page = fetchUrl(url).decode("utf-8", errors="replace")
    papers = []
    for block in page.split('<div class="gs_ri">')[1:]:
        heading = re.search(r'<h3 class="gs_rt"[^>]*>(.*?)</h3>', block, re.DOTALL)
        link = re.search(r'<a [^>]*href="([^"]+)"[^>]*>(.*?)</a>', heading.group(1), re.DOTALL) if heading else None
        if not link:
            continue
        details = re.search(r'<div class="gs_a">(.*?)</div>', block, re.DOTALL)
        snippet = re.search(r'<div class="gs_rs">(.*?)</div>', block, re.DOTALL)
        summary = ". ".join(stripTags(match.group(1)) for match in (details, snippet) if match)
        papers.append({"title": stripTags(link.group(2)), "summary": summary[:SUMMARY_LENGTH],
                       "link": urllib.parse.urljoin(url, html.unescape(link.group(1)))})
    if not papers:
        raise ValueError("Google Scholar did not show any result. It may be asking for a captcha.")
    return papers[:PAPERS_PER_SEARCH]


def searchArxiv(subject):
    terms = " AND ".join(f"all:{word}" for word in subject.split())
    query = urllib.parse.urlencode({"search_query": terms, "max_results": PAPERS_PER_SEARCH, "sortBy": "relevance"})
    return readFeed(f"https://export.arxiv.org/api/query?{query}", PAPERS_PER_SEARCH)


# publisher is the Crossref number of a publisher (see PAPER_PUBLISHERS in sources_library.py): only its papers are returned.
def searchCrossref(subject, publisher=None):
    parameters = {"query": subject, "rows": PAPERS_PER_SEARCH, "select": "title,abstract,URL"}
    if publisher:
        parameters["filter"] = f"member:{publisher},type:journal-article,type:proceedings-article"
    query = urllib.parse.urlencode(parameters)
    items = json.loads(fetchUrl(f"https://api.crossref.org/works?{query}")).get("message", {}).get("items", [])
    return [{"title": stripTags(" ".join(item["title"])), "summary": stripTags(item.get("abstract", ""))[:SUMMARY_LENGTH], "link": item["URL"]}
            for item in items if item.get("title") and item.get("URL")]


# OpenReview hosts the papers of many machine learning conferences and journals. source=forum keeps the papers and drops the reviews.
def searchOpenReview(subject):
    query = urllib.parse.urlencode({"term": subject, "source": "forum", "limit": PAPERS_PER_SEARCH})
    notes = json.loads(fetchUrl(f"https://api2.openreview.net/notes/search?{query}")).get("notes", [])
    papers = []
    for note in notes:
        title, abstract, venue = (note.get("content", {}).get(field, {}).get("value", "") for field in ("title", "abstract", "venue"))
        if title and abstract and note.get("forum"):
            summary = ". ".join(part for part in (venue, stripTags(abstract)) if part)
            papers.append({"title": stripTags(title), "summary": summary[:SUMMARY_LENGTH], "link": f"https://openreview.net/forum?id={note['forum']}"})
    return papers


PAPER_SEARCHES = {"Google Scholar": searchGoogleScholar, "arXiv": searchArxiv, "Crossref": searchCrossref, "OpenReview": searchOpenReview}


# The publishers that Crossref knows with this name, the biggest first, for a user who does not find theirs in PAPER_PUBLISHERS.
# Each one is {"name", "id", "papers"}, and the id is what LiteratureSurveyLoop takes in publishers.
def findPublishers(name):
    query = urllib.parse.urlencode({"query": name.strip(), "rows": 8})
    items = json.loads(fetchUrl(f"https://api.crossref.org/members?{query}")).get("message", {}).get("items", [])
    found = [{"name": item["primary-name"], "id": item["id"], "papers": item.get("counts", {}).get("total-dois", 0)}
             for item in items if item.get("primary-name") and item.get("id")]
    return sorted(found, key=lambda publisher: -publisher["papers"])


# publishers is {name: Crossref number} (PAPER_PUBLISHERS has the common ones). Each is searched on its own, so the survey can be
# restricted to them, with searches=() for example. Logins are {website: (username, password)} for the pages that ask for an account.
class LiteratureSurveyLoop(Loop):
    def __init__(self, agent, subject, length, searches=tuple(PAPER_SEARCHES), numberOfLoops=5, publishers=None):
        super().__init__(agent, numberOfLoops, "LITERATURE_SURVEY_RULES.md")
        self.subject = subject
        self.length = length
        self.searches = searches
        self.publishers = dict(publishers or {})
        self.papers = []
        self.logins = {}

    def describeTask(self):
        places = [*self.searches, *(f"papers of {name}" for name in self.publishers)]
        return f"Write a literature survey on {self.subject} in at most {self.length} words, from the papers found by: {', '.join(places)}."

    # A search that fails (for example Google Scholar blocking automatic searches) is skipped, the others continue.
    def searchPapers(self):
        searches = [(name, partial(PAPER_SEARCHES[name], self.subject)) for name in self.searches]
        searches += [(name, partial(searchCrossref, self.subject, number)) for name, number in self.publishers.items()]
        papers, titles = [], set()
        for name, search in searches:
            try:
                found = search()
            except FETCH_ERRORS as error:
                self.notifyUser(f"Skipping the search on {name}: {describeError(error)}.")
                continue
            for paper in found:
                title = " ".join(getWords(paper["title"]))
                if title not in titles:
                    titles.add(title)
                    papers.append(paper)
        if searches and not papers:
            self.checkOnline("the papers were searched")
        return papers

    # A link like doi.org/... is redirected to the publisher, so the account is asked for (and used on) the publisher that refused.
    # Each publisher is asked for an account at most once, and a refused account is dropped to avoid locking it.
    def downloadPaperPage(self, link):
        try:
            return fetchUrl(link, self.logins.get(urllib.parse.urlparse(link).netloc))
        except urllib.error.HTTPError as error:
            if error.code not in (401, 403):
                raise
            link = error.url or link
            host = urllib.parse.urlparse(link).netloc
            if host not in self.logins:
                self.logins[host] = self.askLogin(host)
            if self.logins[host] is None:
                raise
        try:
            return fetchUrl(link, self.logins[host])
        except urllib.error.HTTPError:
            self.logins[host] = None
            raise

    def readPublisherPages(self):
        for paper in self.papers:
            if len(paper["summary"]) >= MIN_ABSTRACT_LENGTH:
                continue
            try:
                abstract = readAbstract(self.downloadPaperPage(paper["link"]))
            except FETCH_ERRORS as error:
                self.notifyUser(f"Could not open {paper['link']}: {describeError(error)}. Using the short description of the search.")
                continue
            if len(abstract) > len(paper["summary"]):
                paper["summary"] = abstract[:SUMMARY_LENGTH]

    # Every link in the survey must come from the search. This catches invented sources.
    def checkSurvey(self, draft):
        links = [paper["link"] for paper in self.papers]
        cited = [link.rstrip(".") for link in re.findall(r"https?://[^\s)\]>,;]+", draft)]
        unknown = [link for link in cited if link not in links]
        if not cited:
            return "Cite the papers you use with their exact links."
        if unknown:
            return f"These links are not in the paper list: {unknown}. Cite only these links: {links}"
        return checkLength(draft, self.length)

    def run(self):
        self.papers = self.keepTrying(self.searchPapers)
        if not self.papers:
            self.notifyUser(f"No papers found for {self.subject}.")
            return None
        self.readPublisherPages()
        task = prompts.LITERATURE_PROMPT.format(subject=self.subject, length=self.length, papers=formatItems(self.papers))
        survey = self.reviewLoop(task, self.checkSurvey, key="survey")
        if survey is None:
            self.notifyUser("The survey was not saved.")
            return None
        addToContext("literature_contexts.json", [self.subject], {"survey": survey, "sources": self.papers})
        self.saveResult("literature_survey", survey)
        self.logAction(f"Saved the literature survey on {self.subject}.")
        self.notifyUser("The survey is saved.")
        return survey


# ==============
# Document formatter harness. The original file is never changed, a new _formatted file is written.
# ==============
class DocumentFormatLoop(Loop):
    def __init__(self, agent, filePath, style, numberOfLoops=5):
        super().__init__(agent, numberOfLoops, "DOCUMENT_FORMAT_RULES.md")
        self.path = Path(filePath)
        self.style = style
        self.original = self.path.read_text(encoding="utf-8")

    def describeTask(self):
        return f"Format the document {self.path.name} in this style: {self.style}, without changing its content."

    def files(self):
        return [self.path]

    # Counter subtraction keeps only the words that are in the original but missing from the draft.
    def checkContent(self, draft):
        lost = Counter(getWords(self.original)) - Counter(getWords(draft))
        if sum(lost.values()) > len(getWords(self.original)) * 0.02:
            return f"You changed or removed words of the original, for example {list(lost)[:10]}. Keep every word, only change the layout."
        return ""

    def run(self):
        task = prompts.DOCUMENT_FORMAT_PROMPT.format(style=self.style, document=self.original)
        formatted = self.reviewLoop(task, self.checkContent, key="formatted")
        if formatted is None:
            self.notifyUser("The document was not saved.")
            return None
        output = self.path.with_name(f"{self.path.stem}_formatted{self.path.suffix}")
        output.write_text(formatted, encoding="utf-8")
        self.logAction(f"Wrote the formatted copy {output}.")
        self.notifyUser(f"The formatted document is saved as {output}.")
        return formatted


# ==============
# Math checker harness. It referees a research text: its proofs, assumptions and logic. Arithmetic is not its job.
# ==============
FINDING_PATTERN = re.compile(r"^CLAIM:(.*?)^QUOTE:(.*?)^STATUS:(.*?)^WHY:(.*?)(?=^CLAIM:|^VERDICT:|\Z)", re.MULTILINE | re.DOTALL)
STATEMENT_PATTERN = re.compile(r"\\begin\{(?:theorem|lemma|proposition|corollary|claim)\}|"
                               r"^\W*(?:theorem|lemma|proposition|corollary|claim)\s+\d", re.MULTILINE | re.IGNORECASE)


class MathCheckLoop(Loop):
    def __init__(self, agent, filePath, numberOfLoops=5):
        super().__init__(agent, numberOfLoops, "MATH_CHECK_RULES.md")
        self.path = Path(filePath)
        self.original = self.path.read_text(encoding="utf-8")

    def describeTask(self):
        return f"Referee the mathematical text {self.path.name}: check its theorems, proofs and logic, and report every gap or error."

    def files(self):
        return [self.path]

    # Returns a list of (claim, quote, status, why), or an empty list if a finding is badly written.
    def parseFindings(self, report):
        findings = [(claim.strip(), quote.strip(), status.strip().upper(), why.strip())
                    for claim, quote, status, why in FINDING_PATTERN.findall(report)]
        return findings if len(findings) == len(re.findall(r"^CLAIM:", report, re.MULTILINE)) else []

    # A second agent call, which only sees one finding, tries to refute it. This removes false alarms.
    def challenge(self, quote, why):
        answer = self.askAgent("You are a second, skeptical referee. A reviewer says the passage below has a problem. "
                               "Check it carefully against the whole document. Reply CONFIRMED if it really has this problem, "
                               f"otherwise reply REJECTED and explain why.\nPassage: {quote}\nProblem: {why}\n\nDocument:\n{self.original}")
        return "" if answer.strip().upper().startswith("CONFIRMED") else answer

    def checkReport(self, report):
        verdict = re.search(r"^VERDICT:\s*(NO PROBLEMS FOUND|PROBLEMS FOUND)", report, re.MULTILINE | re.IGNORECASE)
        findings = self.parseFindings(report)
        if not findings or not verdict:
            return f"Write every finding as plain lines, exactly like this:\n{prompts.FINDING_FORMAT}\nThen finish with VERDICT: NO PROBLEMS FOUND or VERDICT: PROBLEMS FOUND."
        document = "".join(self.original.split())
        for claim, quote, status, why in findings:
            if status not in ("VALID", "GAP", "ERROR", "UNCLEAR") or not why:
                return f"The finding '{claim}' needs a STATUS (VALID, GAP, ERROR or UNCLEAR) and a WHY."
            # Whitespace is ignored, so a quote may be broken over several lines. An invented quote means an invented finding.
            if "".join(quote.strip('"“”`').split()) not in document:
                return f"The quote of '{claim}' is not in the document. Copy one passage exactly, character for character."
        statements = len(STATEMENT_PATTERN.findall(self.original))
        if len(findings) < statements:
            return f"The document states {statements} theorems, lemmas, propositions or claims, but you wrote only {len(findings)} findings. Review all of them."
        foundProblems = any(status != "VALID" for claim, quote, status, why in findings)
        saysProblems = verdict.group(1).upper() == "PROBLEMS FOUND"
        if foundProblems != saysProblems:
            return "Your VERDICT does not match your findings. It is NO PROBLEMS FOUND only if every STATUS is VALID."
        for claim, quote, status, why in findings:
            rejection = self.challenge(quote, why) if status in ("GAP", "ERROR") else ""
            if rejection:
                return f"A second referee rejected your finding '{claim}': {rejection}\nExamine the passage again. If you were wrong, change it to VALID."
        return ""

    def run(self):
        task = prompts.MATH_CHECK_PROMPT.format(findingFormat=prompts.FINDING_FORMAT, document=self.original)
        report = self.reviewLoop(task, self.checkReport, key="report")
        if report is None:
            self.notifyUser("The report was not saved.")
            return None
        output = self.path.with_name(f"{self.path.stem}_math_report.md")
        output.write_text(report, encoding="utf-8")
        self.logAction(f"Wrote the report {output}.")
        self.notifyUser(f"The report is saved as {output}.")
        return report


# ==============
# Coding harness. The code is only written and run after the user agrees.
# ==============
class CoderLoop(Loop):
    def __init__(self, agent, task, filePath, testCommand=None, numberOfLoops=5):
        super().__init__(agent, numberOfLoops, "CODER_RULES.md")
        self.task = task
        self.path = Path(filePath)
        self.testCommand = testCommand or [sys.executable, str(self.path.resolve())]

    def describeTask(self):
        return f"Write the code of {self.path.name} for this task: {self.task}. It is checked by running: {' '.join(self.testCommand)}"

    def files(self):
        return [self.path]

    # The file holds a draft from the first test on. The original is in the progress, which is saved before the first draft is written.
    def runTests(self, draft):
        if not self.progress.get("touched"):
            self.remember("touched", True)
        self.path.write_text(stripFences(draft), encoding="utf-8")
        try:
            result = subprocess.run(self.testCommand, capture_output=True, text=True, timeout=RUN_TIMEOUT, cwd=self.folder)
        except subprocess.TimeoutExpired:
            return f"The code ran for more than {RUN_TIMEOUT} seconds. Fix any endless loop or make it faster."
        if result.returncode == 0:
            return ""
        return f"Running {' '.join(self.testCommand)} failed with:\n{(result.stdout + result.stderr)[-2000:]}"

    def rollback(self):
        if not self.progress.get("touched") or self.progress.get("kept"):
            return
        original = self.progress["original"]
        if original is None:
            self.path.unlink(missing_ok=True)
        else:
            self.path.write_text(original, encoding="utf-8")
        self.remember("touched", False)

    def describePending(self):
        if self.progress.get("touched") and not self.progress.get("kept"):
            return f"{self.path} holds a draft of the code that the user did not approve yet. Stopping puts the original back."
        return ""

    def run(self):
        command = " ".join(self.testCommand)
        if not isYes(self.askUser(f"I will write code into {self.path} and run: {command}\nThis changes files on your computer. Continue? (yes/no)")):
            return None
        if not (self.resumed and "original" in self.progress):
            self.progress.update(touched=False, kept=False)
            self.remember("original", self.path.read_text(encoding="utf-8") if self.path.exists() else None)
        original = self.progress["original"]
        task = prompts.CODER_PROMPT.format(fileName=self.path.name, task=self.task, command=command,
                                           current=original if original is not None else "the file does not exist yet")
        try:
            code = self.reviewLoop(task, self.runTests, key="code")
        except BaseException:
            self.rollback()
            raise
        if code is None:
            self.rollback()
            self.notifyUser(f"No code was kept, {self.path} is back to how it was.")
            return None
        self.path.write_text(stripFences(code), encoding="utf-8")
        self.remember("kept", True)
        self.logAction(f"Wrote the code of {self.path}, checked by running: {command}")
        self.notifyUser(f"The code is saved in {self.path}.")
        return stripFences(code)


# ==============
# Saved swarms. While a swarm runs, its state is written to RUNS_FOLDER (inside agent-files) at every change and every few seconds,
# so a swarm that was interrupted (internet lost, computer turned off...) can go on later. Passwords, API keys and accounts are never saved.
# ==============
def swarmStatePath(swarmId):
    return AGENT_FILES / RUNS_FOLDER / f"swarm_{swarmId}.json"


# The file is written under another name and then renamed, so a computer that stops in the middle of a write never leaves a broken file.
def saveSwarmState(swarmId, state):
    path = swarmStatePath(swarmId)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    with open(temporary, "w", encoding="utf-8") as file:
        file.write(json.dumps(state, indent=2, ensure_ascii=False, default=str))
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)


def clearSwarmState(swarmId):
    swarmStatePath(swarmId).unlink(missing_ok=True)


# The swarms that did not end, newest first: their saved states, with running: True if another program is working on one right now.
# A file that cannot be read is left alone. The swarm of this very program is not listed, because it is not interrupted.
def findUnfinishedSwarms():
    found = []
    for path in (AGENT_FILES / RUNS_FOLDER).glob("swarm_*.json"):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if state["version"] != STATE_VERSION or not state["members"] or not isinstance(state["members"], dict):
                continue
            alive = state["state"] in ("running", "paused") and datetime.now().timestamp() - state["heartbeat"] < STALE_SECONDS
            if alive and state["pid"] == os.getpid():
                continue
        except (OSError, ValueError, KeyError, TypeError):
            continue
        state["running"] = alive
        found.append(state)
    return sorted(found, key=lambda state: state["heartbeat"], reverse=True)


# ==============
# MAJOR: Swarm harness. Multiple harnesses/models interacting with each other.
# An agent of the swarm is any loop above. The first agent added is the leader.
# The leader decides who waits for whom (waitsFor, or planWithLeader). The agents that wait for nobody
# work at the same time. An agent only starts after the agents it waits for finished, and receives their results.
# The leader works last. Each agent reports to the leader when it is done. getStages shows the groups of agents that work at the same time.
# The status of an agent is waiting, working, done or failed. Only the agents that are working are active.
# The get methods are made for a user interface to show the swarm as a tree and to follow it while it runs.
# When the user clicks an agent of the tree (the leader too), sendUserMessage gives it a message without stopping it.
# The message is logged with the sender USER_NAME, so getMessages(USER_NAME, name) shows what the user told an agent.
# All the messages are cleared when the swarm runs again.
#
# The whole swarm is in plan mode or in execute mode (setMode), and each agent plans or executes its own task.
# In both modes an agent writes a draft (its plan, or what it is about to do) and waits for the user. Nothing is done before the approval.
# The review of an agent is "ready" while its draft waits for the user (a light a user interface can turn green), then approved or rejected.
# The model of an agent (getModelInfo in models_library.py) is chosen when it is added. Every local model needs VRAM, and the swarm needs
# the sum of them. checkModel tells before the choice if the GPUs can host the model, and getVramStatus shows the total while the swarm is built.
# A model that does not fit in the GPUs cannot be chosen (the user changes to a smaller one with setModel, or uses an API model).
# One that fits but not in the memory that is free now can be chosen with a warning, and run refuses until the memory is free.
# The user can approve (approveDraft), reject (rejectDraft) or correct (correctDraft) an agent on its own by clicking on its name.
# An approved agent goes on right away, without waiting for the others, and the agents that wait for it start when it is done.
# When no agent is busy, the leader summarises where every agent stands, what the user already approved included, and the user
# approves, rejects or corrects the summary as a whole, or types the name of an agent to decide about it alone.
# A correction of the summary is sent by the leader to the agents it concerns. A correction of one agent (correctDraft, or a message
# to it) only changes the draft of that agent. In both cases the leader summarises again afterwards.
# A user interface follows the swarm live with addListener: every change (the status or the review of an agent, a message, a summary)
# is given to the listeners as an event. getTree has, for every agent, who it waits for (waitsFor), who it still waits for (waitingOn),
# and the time it is scheduled for (startAt, for an agent with a time of its own, like the news briefer, which startNow wakes up).
#
# The work of the user is never lost. While the swarm runs its whole state is saved at every change (see Saved swarms above), so a swarm that
# was interrupted is found again with findUnfinishedSwarms, brought back with restore (the user interface builds the agents again), and
# goes on where it stopped with resume. The agents remember what the user approved, so nothing is asked or sent twice.
# If the internet is lost while an agent works, the agent is paused (status paused), not failed, and the leader tells the user, who
# continues (continueWork, which tries again) or cancels. To cancel, the leader summarises every change that was made (summarizeChanges)
# and the user confirms (stopWork) or goes on. startInBackground runs the swarm in its own thread, followed with isRunning, wait and outcome.
# ==============
class Swarm:
    def __init__(self, mission):
        self.mission = mission
        self.members = {}
        self.leader = None
        self.messages = []
        self.mode = "execute"
        self.summary = ""
        self.listeners = []
        # Everything about the drafts waiting for the user changes under this condition, which the waiting threads sleep on.
        self.changed = threading.Condition()
        self.id = f"{datetime.now():%Y%m%d_%H%M%S}_{os.urandom(2).hex()}"
        self.stopped = False
        self.interruption = None
        # active is True while the swarm runs. The snapshots are numbered, so an old one is never saved over a newer one.
        self.active = False
        self.snapshots = 0
        self.saved = 0
        self.saveFailed = False
        self.heartbeat = threading.Event()
        self.thread = None
        self.outcome = {}

    def getMember(self, name):
        if name not in self.members:
            raise ValueError(f"There is no agent called {name} in the swarm.")
        return self.members[name]

    # A listener is a function that takes an event: a dictionary with the kind (status, review, message, summary, scheduled, run, finished,
    # connectionLost, resumed or stopped), the agent it is about, the time, and details. A listener that fails is reported but never stops the swarm.
    def addListener(self, listener):
        self.listeners.append(listener)

    def emit(self, kind, agent="", **details):
        event = {"kind": kind, "agent": agent, "time": f"{datetime.now():%H:%M:%S}", **details}
        self.checkpoint()
        for listener in list(self.listeners):
            try:
                listener(event)
            except Exception:
                traceback.print_exc()

    # What is saved of every agent. The loop of the agent saves its own state, and the recipe is what the user interface needs to build it again.
    def describeState(self):
        members = {}
        for name, member in self.members.items():
            start = member["startAt"]
            members[name] = {**{field: member[field] for field in SAVED_FIELDS}, "waitsFor": list(member["waitsFor"]),
                             "startAt": f"{start:%Y-%m-%d %H:%M}" if start else None, "state": member["agent"].getState()}
        return {"version": STATE_VERSION, "id": self.id, "mission": self.mission, "mode": self.mode, "leader": self.leader,
                "state": "paused" if self.interruption else "running", "heartbeat": datetime.now().timestamp(), "pid": os.getpid(),
                "savedAt": f"{datetime.now():%Y-%m-%d %H:%M:%S}", "summary": self.summary, "messages": list(self.messages), "members": members}

    # Writes the state to the disk. Whatever goes wrong, the swarm goes on, and the user is told once that the work cannot be continued after a stop.
    # The user is told from another thread, because speaking to the user can wait for a long time, and the locks of the swarm may be held here.
    def checkpoint(self):
        if not self.active:
            return
        failure = None
        try:
            with self.changed:
                self.snapshots += 1
                number, state = self.snapshots, self.describeState()
            with STATE_LOCK:
                if not self.active or number <= self.saved:
                    return
                self.saved = number
                saveSwarmState(self.id, state)
        except Exception as error:
            failure = error
        if failure and not self.saveFailed:
            self.saveFailed = True
            warning = f"Warning: the state of the swarm could not be saved ({failure}). If the program stops, its work cannot be continued."
            threading.Thread(target=self.getMember(self.leader)["agent"].notifyUser, args=(warning,), daemon=True).start()

    def setStatus(self, name, status):
        self.members[name]["status"] = status
        self.emit("status", name, status=status)

    # boss is the agent this one reports to in the tree (the leader if not given).
    # waitsFor lists the agents whose results this one needs before it can start. It is not used for the leader.
    # model is what getModelInfo gives for the model of the agent. A local model that does not fit in the GPUs is refused.
    # recipe is what the user interface needs to build the agent again after the program stopped (without passwords, they are never saved).
    def addAgent(self, name, agent, role, task, boss=None, waitsFor=(), model=None, recipe=None):
        if name in self.members:
            raise ValueError(f"There is already an agent called {name} in the swarm.")
        if name == USER_NAME:
            raise ValueError(f"{USER_NAME} is the name of the user, choose another name for the agent.")
        if boss:
            self.getMember(boss)
        check = self.checkModel(model) if model else {"allowed": True}
        if not check["allowed"]:
            raise ValueError(check["message"])
        self.members[name] = self.newMember(name, agent, role, task, boss, waitsFor, model, recipe)
        self.leader = self.leader or name

    def newMember(self, name, agent, role, task, boss, waitsFor, model, recipe):
        return {"name": name, "agent": agent, "role": role, "task": task, "boss": boss, "waitsFor": list(waitsFor), "model": model, "recipe": recipe,
                "status": "waiting", "result": None, "error": "", "mode": self.mode, "review": "", "draft": "", "problem": "",
                "decision": None, "revision": 0, "startAt": None, "started": False, "resumeStart": None, "wake": threading.Event()}

    # An agent that did not finish starts again, with what its loop remembers. The ones that finished (done or failed) stay as they are.
    def settle(self, member):
        if member["status"] not in ("done", "failed"):
            member.update(status="waiting", error="", review="", draft="", problem="", decision=None, startAt=None)

    # Brings back a swarm found by findUnfinishedSwarms. makeAgent(name, saved) gives the loop of an agent, built again from saved["recipe"]
    # and saved["model"] (the passwords are asked again, because they are never saved). Then resume() goes on where the swarm stopped.
    @classmethod
    def restore(cls, saved, makeAgent):
        swarm = cls(saved["mission"])
        swarm.id, swarm.mode, swarm.leader = saved["id"], saved["mode"], saved["leader"]
        swarm.summary, swarm.messages = saved.get("summary", ""), list(saved.get("messages", []))
        for name, data in saved["members"].items():
            agent = makeAgent(name, data)
            agent.setState(data.get("state", {}))
            member = swarm.newMember(name, agent, data["role"], data["task"], data["boss"], data["waitsFor"], data["model"], data.get("recipe"))
            member.update({field: data[field] for field in SAVED_FIELDS if field in data})
            member["resumeStart"] = datetime.strptime(data["startAt"], "%Y-%m-%d %H:%M") if data.get("startAt") else None
            swarm.settle(member)
            swarm.members[name] = member
        return swarm

    # Goes on with a swarm that was interrupted (one that was restored, or one whose run was cut short): what is done stays done,
    # and the agents that did not finish start again, remembering what the user approved.
    def resume(self):
        return self.run(resume=True)

    def setLeader(self, name):
        self.getMember(name)
        self.leader = name

    # The mode is used the next time the swarm runs.
    def setMode(self, mode):
        if mode not in ("plan", "execute"):
            raise ValueError("The mode is plan or execute.")
        self.mode = mode

    def getMode(self):
        return self.mode

    # The VRAM in GB that the models of the agents need together, without the agent called replacing.
    def getNeededVram(self, replacing=None):
        return round(sum(member["model"]["vram"] for name, member in self.members.items() if member["model"] and name != replacing), 1)

    # What the user sees before choosing a model for an agent, or to replace the model of the agent called replacing.
    # A local model that does not fit in the GPUs is not allowed, and the message says why. One that fits, but not in the memory
    # that is free now, is allowed and the message is a warning. API models are always allowed, but they do not hide the warning of the swarm.
    def checkModel(self, model, replacing=None):
        taken = self.getNeededVram(replacing)
        after = round(taken + model["vram"], 1)
        status = checkVram(after, readGpus() if after else [])
        name, vram, total = model["name"], model["vram"], status["total"]
        if model["local"] and status["gpus"] and vram > total:
            message = f"{name} needs about {vram} GB of VRAM, but your GPUs only have {total} GB in total. Choose a smaller model or an API model."
        elif model["local"] and status["gpus"] and not status["fits"]:
            message = (f"{name} needs about {vram} GB of VRAM. The other agents already take {taken} GB and your GPUs have {total} GB in total, "
                       "so there is no room left. Choose a smaller model or an API model.")
        else:
            message = status["message"]
        return {"allowed": status["fits"] or not model["local"], "vram": vram, "needed": after, "message": message}

    # Changes the model of an agent, for example to a smaller one. agent is the new loop that uses it, if the user interface made one.
    def setModel(self, name, model, agent=None):
        member = self.getMember(name)
        check = self.checkModel(model, replacing=name)
        if not check["allowed"]:
            raise ValueError(check["message"])
        member["model"] = model
        if agent is not None:
            member["agent"] = agent

    # The VRAM the swarm needs so far next to what the GPUs have, for the user interface to show while the swarm is built.
    # The message is empty, or the warning that the swarm cannot run now.
    def getVramStatus(self):
        return checkVram(self.getNeededVram(), readGpus())

    def checkGpus(self):
        needed = self.getNeededVram()
        status = checkVram(needed, readGpus() if needed else [])
        if not status["runnable"]:
            raise ValueError(status["message"])

    def setWaitsFor(self, name, waitsFor):
        self.getMember(name)["waitsFor"] = list(waitsFor)

    def getLeader(self):
        return self.leader

    def getAgents(self):
        return list(self.members)

    def getStatuses(self):
        return {name: member["status"] for name, member in self.members.items()}

    def getStatus(self, name):
        return self.getMember(name)["status"]

    def getActiveAgents(self):
        return [name for name, status in self.getStatuses().items() if status == "working"]

    def getInactiveAgents(self):
        return [name for name, status in self.getStatuses().items() if status != "working"]

    # The agents with a draft waiting for the user, the ones a user interface shows with a green light.
    def getReadyAgents(self):
        return [name for name, member in self.members.items() if member["review"] == "ready"]

    # The latest summary of the leader, for the user interface to show next to the tree.
    def getSummary(self):
        return self.summary

    def getRole(self, name):
        return self.getMember(name)["role"]

    def getTask(self, name):
        return self.getMember(name)["task"]

    def getParent(self, name):
        member = self.getMember(name)
        return None if name == self.leader else (member["boss"] or self.leader)

    def getChildren(self, name):
        return [other for other in self.members if self.getParent(other) == name]

    # The agents this one is waiting for right now. The leader works last, so it waits for all the others.
    # In plan mode nobody waits for anybody.
    def getWaitingOn(self, name):
        member = self.getMember(name)
        if member["status"] != "waiting" or member["mode"] == "plan":
            return []
        others = [other for other in self.members if other != self.leader] if name == self.leader else member["waitsFor"]
        return [other for other in others if self.members[other]["status"] not in ("done", "failed")]

    def getStartAt(self, name):
        start = self.getMember(name)["startAt"]
        return f"{start:%Y-%m-%d %H:%M}" if start else ""

    def getInfo(self, name):
        member = self.getMember(name)
        return {"name": name, "role": member["role"], "task": member["task"], "status": member["status"], "boss": self.getParent(name),
                "waitsFor": member["waitsFor"], "waitingOn": self.getWaitingOn(name), "startAt": self.getStartAt(name), "isLeader": name == self.leader,
                "result": member["result"], "error": member["error"], "model": member["model"], "mode": member["mode"], "review": member["review"],
                "draft": member["draft"], "problem": member["problem"], "revision": member["revision"], "plan": member["agent"].approvedPlan,
                "actions": list(member["agent"].actions)}

    # Who lost the connection and why ({agent: reason}) while the swarm waits for the user to continue or to cancel, otherwise None.
    def getInterruption(self):
        return dict(self.interruption["agents"]) if self.interruption else None

    # The whole swarm as nested dictionaries, starting from the leader, ready to be drawn as a tree.
    def getTree(self, name=None):
        if not self.members:
            return {}
        name = name or self.leader
        member = self.getMember(name)
        return {"name": name, "role": member["role"], "task": member["task"], "status": member["status"], "review": member["review"], "model": member["model"],
                "waitsFor": [] if name == self.leader else member["waitsFor"], "waitingOn": self.getWaitingOn(name), "startAt": self.getStartAt(name),
                "children": [self.getTree(child) for child in self.getChildren(name)]}

    def getMessages(self, sender=None, receiver=None):
        return [message for message in self.messages if sender in (None, message["sender"]) and receiver in (None, message["receiver"])]

    # Who talks to whom and how many messages were sent, for drawing the arrows between the agents.
    def getConnections(self):
        counts = Counter((message["sender"], message["receiver"]) for message in self.messages if message["sender"] != USER_NAME)
        return [{"from": sender, "to": receiver, "count": count} for (sender, receiver), count in counts.items()]

    # waits is {agent: [agents it waits for]}. Returns the groups of agents that work at the same time, one group after the other.
    def findStages(self, waits):
        stages, done = [], set()
        while len(done) < len(waits):
            stage = [name for name in waits if name not in done and all(other in done for other in waits[name])]
            if not stage:
                raise ValueError(f"These agents wait for each other in a circle, so none of them can start: {[name for name in waits if name not in done]}")
            stages.append(stage)
            done.update(stage)
        return stages

    # The agents (without the leader, who works last) that can work at the same time, group after group.
    def getStages(self):
        if not self.members:
            raise ValueError("The swarm has no agents.")
        waits = {name: member["waitsFor"] for name, member in self.members.items() if name != self.leader}
        for name, others in waits.items():
            for other in others:
                if other not in waits:
                    raise ValueError(f"{name} cannot wait for {other}. It is not an agent of the swarm, or it is the leader, who works last.")
        return self.findStages(waits)

    def checkPlan(self, draft):
        workers = [name for name in self.members if name != self.leader]
        try:
            plan = json.loads(stripFences(draft))
            waits = {name: list(plan[name]) for name in workers}
        except (ValueError, KeyError, TypeError):
            return f"Reply only with JSON like {prompts.SWARM_PLAN_EXAMPLE}, with one entry for each of these agents: {workers}"
        unknown = [other for others in waits.values() for other in others if other not in workers]
        if unknown:
            return f"These are not agents you can wait for: {unknown}. Choose from {workers}"
        try:
            self.findStages(waits)
        except ValueError as error:
            return str(error)
        return ""

    # The leader decides who waits for whom, and the user approves its plan like any other draft.
    def planWithLeader(self):
        leader = self.getMember(self.leader)["agent"]
        agents = "\n".join(f"- {name}: {member['role']}. Task: {member['task']}" for name, member in self.members.items() if name != self.leader)
        plan = leader.reviewLoop(prompts.SWARM_PLAN_PROMPT.format(mission=self.mission, agents=agents, example=prompts.SWARM_PLAN_EXAMPLE), self.checkPlan, own=False)
        if plan is None:
            return False
        for name, others in json.loads(stripFences(plan)).items():
            if name != self.leader:
                self.setWaitsFor(name, others)
        return True

    # The picture of the swarm that an agent receives: the mission, its own role and task, and who else is in the swarm.
    def describe(self, name):
        member = self.getMember(name)
        team = "\n".join(f"- {other}{' (leader)' if other == self.leader else ''}: {info['role']}. Task: {info['task']}"
                         f"{' Waits for: ' + ', '.join(info['waitsFor']) + '.' if info['waitsFor'] and other != self.leader else ''}"
                         for other, info in self.members.items())
        return f"Mission of the swarm: {self.mission}\nYou are {name}, the {member['role']}. Your task: {member['task']}\nThe agents of the swarm:\n{team}"

    def logMessage(self, sender, receiver, message):
        self.messages.append({"time": f"{datetime.now():%H:%M:%S}", "sender": sender, "receiver": receiver, "message": message})
        self.emit("message", receiver, sender=sender, receiver=receiver, message=message)

    def communicate(self, sender, receiver, message):
        self.getMember(sender)
        self.getMember(receiver)["agent"].receive(sender, str(message))
        self.logMessage(sender, receiver, str(message))

    # The agent reads the message with its next prompt, so it works while the agent is waiting or working.
    # An agent that is done or failed cannot read it anymore, so the user is told instead of being ignored silently.
    # If the draft of the agent is waiting for the user, the message is taken as a correction and the agent writes the draft again.
    def sendUserMessage(self, name, message):
        member = self.getMember(name)
        message = str(message).strip()
        if not message:
            raise ValueError("The message is empty.")
        if member["status"] in ("done", "failed"):
            raise ValueError(f"{name} has already finished, so it cannot read new messages.")
        member["agent"].receiveFromUser(message)
        self.logMessage(USER_NAME, name, message)
        self.askCorrection(member)

    # Gives the answer of the user to the agent that waits for it. It must be called with self.changed held.
    def decide(self, member, reply):
        member["decision"] = reply
        member["review"] = "approved" if isYes(reply) else "rejected" if isNo(reply) else ""
        self.changed.notify_all()
        self.emit("review", member["name"], review=member["review"])

    def askCorrection(self, member):
        with self.changed:
            if member["review"] == "ready":
                self.decide(member, CORRECTION_REPLY)

    # The reviewer of every agent while the swarm runs. The agent sleeps here until the user decides about its draft.
    # The revision counts the drafts of the agent, so an answer can be tied to the draft it was given for.
    def waitForReview(self, name, draft, problem):
        member = self.getMember(name)
        with self.changed:
            if self.stopped:
                raise SwarmStopped(STOPPED_MESSAGE)
            member.update(review="ready", draft=draft, problem=problem, decision=None, revision=member["revision"] + 1)
            self.changed.notify_all()
            self.emit("review", name, review="ready")
            self.changed.wait_for(lambda: member["decision"] is not None or self.stopped)
            if member["decision"] is None:
                member["review"] = ""
                raise SwarmStopped(STOPPED_MESSAGE)
            return member["decision"]

    def checkReady(self, name):
        if self.getMember(name)["review"] != "ready":
            raise ValueError(f"{name} has nothing waiting for the user.")

    # What the user does after clicking on the name of an agent whose draft is ready. The agent goes on right away.
    # With a revision, the user decides about the draft that was looked at, and not about a newer one.
    def decideDraft(self, name, reply, revision=None):
        with self.changed:
            self.checkReady(name)
            if revision is not None and self.members[name]["revision"] != revision:
                raise ValueError(f"{name} wrote a new draft since you looked at it.")
            self.decide(self.members[name], reply)

    def approveDraft(self, name, revision=None):
        self.decideDraft(name, "yes", revision)

    def rejectDraft(self, name, revision=None):
        self.decideDraft(name, "no", revision)

    def correctDraft(self, name, comment):
        self.checkReady(name)
        self.sendUserMessage(name, comment)

    def describeDrafts(self, names):
        return "\n".join(f"- {name} ({self.members[name]['role']}): {self.members[name]['draft']}" for name in names)

    def describeMember(self, name, member):
        approved = " and approved by the user" if member["review"] == "approved" else ""
        if member["status"] == "paused":
            state = "paused because the connection was lost"
        elif member["review"] == "rejected":
            state = "rejected by the user"
        elif member["status"] == "failed":
            state = f"failed ({member['error']})"
        elif member["review"] == "ready":
            warning = f" (WARNING, the automatic checks found a problem: {member['problem']})" if member["problem"] else ""
            state = f"waiting for the user{warning}: {member['draft']}"
        elif member["status"] == "done":
            state = f"done{approved}: {str(member['result'])[:SUMMARY_LENGTH]}"
        elif member["review"] == "approved":
            state = f"approved by the user, now at work: {member['draft']}"
        else:
            state = "writing its draft" if member["status"] == "working" else "has not started yet"
        return f"- {name} ({member['role']}): {state}"

    # Where every agent stands. The leader writes its summary from it, so what the user approved on its own is echoed in the summary.
    def describeProgress(self):
        return "\n".join(self.describeMember(name, member) for name, member in self.members.items())

    # The facts for a user who may stop the swarm: where every agent stands, what it changed outside of itself (an email sent, a file
    # written...), and what it would leave half done.
    def describeChanges(self):
        lines = []
        for name, member in self.members.items():
            lines.append(self.describeMember(name, member))
            lines += [f"    changed, {action['time']}: {action['text']}" for action in member["agent"].actions]
            pending = member["agent"].describePending() if member["status"] != "done" else ""
            if pending:
                lines.append(f"    half done: {pending}")
        return "\n".join(lines)

    def checkSummary(self, draft, names):
        missing = [name for name in names if name not in draft]
        return f"Name every agent, you forgot: {missing}" if missing else ""

    # The summary names every agent that started. If the leader cannot write it, the state of the agents is shown as it is, so the user can always decide.
    # A swarm of one agent has nothing to summarise: the user reads the draft itself, instead of a summary of it.
    def writeSummary(self, request):
        leader = self.getMember(self.leader)
        progress = self.describeProgress()
        started = [name for name, member in self.members.items() if member["status"] != "waiting"]
        template = prompts.PLAN_SUMMARY_PROMPT if leader["mode"] == "plan" else prompts.EXECUTION_SUMMARY_PROMPT
        summary = None
        if len(self.members) > 1:
            try:
                prompt = template.format(mission=self.mission, agents=progress, request=request or "nothing")
                summary = leader["agent"].reviewLoop(prompt, lambda draft: self.checkSummary(draft, started), ask=False, own=False)
            except Exception:
                summary = None
            if summary is None:
                leader["agent"].notifyUser("The leader could not write the summary, so the state of every agent is shown as it is.")
        self.summary = summary or progress
        self.emit("summary", self.leader, text=self.summary)
        return self.summary

    def checkRoute(self, draft, names):
        try:
            route = json.loads(stripFences(draft))
            valid = isinstance(route, dict) and all(isinstance(text, str) and text.strip() for text in route.values())
        except ValueError:
            valid = False
        if not valid:
            return f"Reply only with JSON like {prompts.CORRECTION_ROUTE_EXAMPLE}, or with {{}} if no agent is concerned."
        unknown = [name for name in route if name not in names]
        return f"These are not agents you can ask: {unknown}. Choose from {names}" if unknown else ""

    # The leader decides which agents the correction concerns. If it cannot, all of them get it, and each applies what concerns it.
    def routeCorrection(self, names, comment):
        prompt = prompts.CORRECTION_ROUTE_PROMPT.format(mission=self.mission, correction=comment, agents=self.describeDrafts(names),
                                                        example=prompts.CORRECTION_ROUTE_EXAMPLE)
        try:
            route = self.getMember(self.leader)["agent"].reviewLoop(prompt, lambda draft: self.checkRoute(draft, names), ask=False, own=False)
        except Exception:
            route = None
        return json.loads(stripFences(route)) if route is not None else {name: comment for name in names}

    # The leader messages the agents the correction concerns, and they write their drafts again.
    # If no agent is concerned the correction is about the summary itself, and it is returned to write the summary again.
    def correctWithLeader(self, names, comment, what):
        route = self.routeCorrection(names, comment)
        self.getMember(self.leader)["agent"].notifyUser(f"Asking {', '.join(route)} to correct their {what}." if route else "No agent is concerned, so only the summary is changed.")
        for name, instruction in route.items():
            self.communicate(self.leader, name, f"The user asked for a correction: {comment}\nWhat to change in your {what}: {instruction}")
            self.askCorrection(self.members[name])
        return "" if route else comment

    # What the user does in the console instead of clicking on an agent: look at its draft and decide about it alone.
    def reviewAlone(self, name, revision, what):
        leader = self.getMember(self.leader)["agent"]
        member = self.members[name]
        with USER_LOCK:
            leader.notifyUser(f"[{name}] its {what}:\n{member['draft']}")
            if member["problem"]:
                leader.notifyUser(f"Warning, the automatic checks found a problem: {member['problem']}")
            reply = leader.askUser(f"{name}: type yes to approve, no to reject, or write what you want changed:")
        try:
            if isYes(reply):
                self.approveDraft(name, revision)
            elif isNo(reply):
                self.rejectDraft(name, revision)
            else:
                self.correctDraft(name, reply)
        except ValueError as error:
            leader.notifyUser(f"{error} Your answer was not used.")

    # One round of the user with the leader: the summary of where the agents stand, and the answer of the user.
    # shown is {agent: revision of its draft} for the drafts that wait. An answer only applies to the drafts it was given for,
    # so a draft that changed meanwhile is left out of it and comes back in the next summary.
    def reviewRound(self, shown, request):
        leader = self.getMember(self.leader)["agent"]
        what = "plan" if self.members[self.leader]["mode"] == "plan" else "result"
        summary = self.writeSummary(request)
        with USER_LOCK:
            leader.notifyUser(f"[{self.leader}] Summary of the {what}s:\n{summary}")
            leader.notifyUser(f"You can also check, approve or correct the {what} of each agent by clicking on its name in the swarm.")
            for name in shown:
                if self.members[name]["problem"]:
                    leader.notifyUser(f"Warning for {name}: the automatic checks found a problem with its draft: {self.members[name]['problem']}")
            reply = leader.askUser("Do you approve? Type yes to approve, no to reject, or write what you want changed:" if len(self.members) == 1 else
                                   "Do you approve? Type yes to approve all of them, no to reject all of them, the name of an agent to look at it alone, or write what you want changed:")
        answered = isYes(reply) or isNo(reply)
        with self.changed:
            waiting = [name for name in shown if self.members[name]["review"] == "ready" and self.members[name]["revision"] == shown[name]]
            changed = [name for name in shown if self.members[name]["review"] == "" or self.members[name]["revision"] != shown[name]]
            if answered:
                for name in waiting:
                    self.decide(self.members[name], "yes" if isYes(reply) else "no")
        if changed:
            leader.notifyUser(f"The {what} of {', '.join(changed)} changed while you were reading, so it is not part of your answer. The summary will be updated.")
        alone = next((name for name in waiting if reply.strip().lower() == name.lower()), None)
        if alone:
            self.reviewAlone(alone, shown[alone], what)
        return "" if answered or alone or not waiting else self.correctWithLeader(waiting, reply, what)

    # Called in the thread of an agent that lost its connection. The agent is paused here, with everything it did, until the user decides.
    # The agents that lose the connection together share one question. It returns to try again, or raises SwarmStopped.
    def waitForResume(self, name, error):
        with self.changed:
            if self.stopped:
                raise SwarmStopped(STOPPED_MESSAGE)
            if self.interruption is None:
                self.interruption = {"agents": {}, "decision": None}
            interruption = self.interruption
            interruption["agents"][name] = str(error)
            self.setStatus(name, "paused")
            self.emit("connectionLost", name, reason=str(error))
            self.changed.notify_all()
            self.changed.wait_for(lambda: interruption["decision"] is not None)
            if interruption["decision"] == "stop":
                raise SwarmStopped(STOPPED_MESSAGE)
            self.setStatus(name, "working")

    def resolveInterruption(self, decision):
        with self.changed:
            if self.interruption is None:
                raise ValueError("The swarm is not waiting for a decision about a lost connection.")
            self.interruption["decision"] = decision
            self.interruption = None
            self.changed.notify_all()

    # The user wants to go on after the connection was lost: the agents that were paused try again the step that failed.
    def continueWork(self):
        if self.interruption is None:
            raise ValueError("The swarm is not waiting for a decision about a lost connection.")
        if not isOnline():
            raise ValueError("There is still no internet connection. Check it, then try again.")
        self.resolveInterruption("continue")
        self.emit("resumed")

    # The user confirmed to stop the swarm. The agents that wait are released with SwarmStopped, and the ones at work stop at their next step.
    def stopWork(self):
        with self.changed:
            self.stopped = True
            if self.interruption is not None:
                self.interruption["decision"] = "stop"
                self.interruption = None
            for member in self.members.values():
                member["wake"].set()
            self.changed.notify_all()
        self.emit("stopped")

    # The leader tells the user every change that was made, before the user confirms to stop. If the leader cannot write it (the lost
    # connection is the very reason it is needed), the facts are given as they are.
    def summarizeChanges(self):
        facts = self.describeChanges()
        try:
            summary = self.getMember(self.leader)["agent"].reviewLoop(prompts.CANCEL_SUMMARY_PROMPT.format(mission=self.mission, agents=facts),
                                                                      lambda draft: self.checkSummary(draft, list(self.members)), ask=False, own=False)
        except Exception:
            summary = None
        return summary or f"The leader could not write the summary, so these are the facts:\n{facts}"

    # The user wants to cancel: after the summary, the user confirms to stop there (which can cut the agents in the middle of their task),
    # or goes on until the end.
    def confirmStop(self, leader):
        leader.notifyUser(f"[{self.leader}] Summary of what the swarm did so far:\n{self.summarizeChanges()}")
        leader.notifyUser("If you stop here, everything above stays as it is, but the agents that did not finish are cut in the middle of their task.")
        reply = leader.askUser("Type stop to stop here, or continue to go on until the end:").strip().lower()
        if reply == "stop":
            self.stopWork()
        elif reply == "continue":
            self.continueWork()
        else:
            leader.notifyUser("Nothing was changed, the swarm is still paused.")

    # What the leader asks in the console when the connection is lost. A user interface does the same with continueWork,
    # summarizeChanges and stopWork, and may decide first: the question is then dropped.
    def askAboutInterruption(self, interruption):
        leader = self.getMember(self.leader)["agent"]
        reasons = "\n".join(f"- {name}: {reason}" for name, reason in interruption["agents"].items())
        with USER_LOCK:
            leader.notifyUser(f"[{self.leader}] The swarm lost its connection and is paused. Everything done so far is saved, so nothing is lost.\n{reasons}")
            while self.interruption is interruption:
                reply = leader.askUser("Type continue to try again, or cancel to stop here:").strip().lower()
                if self.interruption is not interruption:
                    return
                try:
                    if reply == "continue":
                        self.continueWork()
                    elif reply == "cancel":
                        self.confirmStop(leader)
                    else:
                        leader.notifyUser("Please type continue or cancel.")
                except ValueError as error:
                    leader.notifyUser(str(error))

    # Ends for good a swarm that is not running (one found on the disk): what the agents that did not finish changed is undone,
    # and the saved state is deleted.
    def abandon(self):
        if self.isRunning():
            raise ValueError("The swarm is running. Stop it first.")
        for member in self.members.values():
            if member["status"] != "done":
                member["agent"].rollback()
        clearSwarmState(self.id)
        self.emit("stopped")

    def isRunning(self):
        return self.active or (self.thread is not None and self.thread.is_alive())

    # For a program that is about to stop while the swarm runs: the state is saved as interrupted, so the next start finds it at once.
    def saveForExit(self):
        if self.active:
            self.closeRun(False)

    # Runs the swarm in its own thread, so the program (or the user interface) stays free while it works. wait() gives the outcome:
    # {"result": ...} or {"error": ...}.
    def startInBackground(self, resume=False):
        if self.isRunning():
            raise ValueError("The swarm is already running.")
        self.outcome = {}
        self.thread = threading.Thread(target=self.runForOutcome, args=(resume,), daemon=True)
        self.thread.start()

    def runForOutcome(self, resume):
        try:
            self.outcome["result"] = self.run(resume)
        except Exception as error:
            self.outcome["error"] = error

    def wait(self, timeout=None):
        if self.thread:
            self.thread.join(timeout)
        return self.outcome

    # An agent is busy while it writes, acts, or is about to start. One that waits for another agent, or for the user, or is finished, is not.
    def isBusy(self, name, waits, thread):
        member = self.members[name]
        if not thread.is_alive():
            return False
        if member["status"] == "waiting":
            return not member["startAt"] and all(self.members[other]["status"] in ("done", "failed") for other in waits[name])
        return member["status"] == "working" and member["review"] != "ready"

    # Runs while a group of agents works. Each time none of them is busy, the leader summarises where they stand and the user decides.
    # An agent the user decided about on its own goes on without waiting for the others, and so do the agents that wait for it.
    # It returns when no draft waits anymore and all the agents are finished. An agent that sleeps until its time is not finished:
    # nothing is left to do until it wakes up (or is started), and then it is looked after like the others.
    # A lost connection comes before everything else: the user decides to continue or to cancel. A stopped swarm returns at once.
    def reviewStage(self, names, threads, waits):
        request = ""
        while True:
            with self.changed:
                self.changed.wait_for(lambda: self.stopped or not any(self.isBusy(name, waits, threads[name]) for name in names))
                if self.stopped:
                    return
                interruption = self.interruption
                shown = {name: self.members[name]["revision"] for name in names if self.members[name]["review"] == "ready"}
                if not shown and not interruption and any(threads[name].is_alive() for name in names):
                    self.changed.wait_for(lambda: any(self.isBusy(name, waits, threads[name]) or self.members[name]["review"] == "ready" for name in names)
                                          or self.interruption or self.stopped or not any(thread.is_alive() for thread in threads.values()))
                    continue
            if interruption:
                self.askAboutInterruption(interruption)
            elif shown:
                request = self.reviewRound(shown, request)
            else:
                return

    # An agent with a time of its own (the news briefer) sleeps until then, without holding the others back. startNow wakes it up.
    # An agent that was already at work when the swarm was interrupted does not wait again, and one that was waiting for its time keeps it.
    def waitForStart(self, name):
        member = self.getMember(name)
        if member["started"]:
            return
        start = member["resumeStart"] or member["agent"].startTime()
        member["resumeStart"] = None
        seconds = (start - datetime.now()).total_seconds() if start else 0
        if seconds > 0:
            member["wake"].clear()
            member["startAt"] = start
            self.emit("scheduled", name, startAt=f"{start:%Y-%m-%d %H:%M}")
            with self.changed:
                self.changed.notify_all()
            member["wake"].wait(seconds)
            member["startAt"] = None
            with self.changed:
                self.changed.notify_all()

    def startNow(self, name):
        member = self.getMember(name)
        if not member["startAt"]:
            raise ValueError(f"{name} is not waiting for a time to start.")
        member["wake"].set()

    def runLeader(self, name):
        self.waitForStart(name)
        self.runMember(name)

    # One failing agent never stops the swarm. Its error is kept and reported to the leader.
    # An agent that is already done or failed, because the swarm was interrupted after that, is not run again.
    def runMember(self, name):
        member = self.getMember(name)
        if member["status"] in ("done", "failed"):
            return
        if self.stopped:
            member["error"] = STOPPED_MESSAGE
            self.setStatus(name, "failed")
            return
        member["started"] = True
        self.setStatus(name, "working")
        try:
            member["result"] = member["agent"].makePlan(member["task"]) if member["mode"] == "plan" else member["agent"].run()
        except SwarmStopped:
            member["error"] = STOPPED_MESSAGE
        except Exception as error:
            member["error"] = f"{type(error).__name__}: {error}"
        if member["result"] is None and not member["error"]:
            member["error"] = "It did not finish, for example because the user did not approve."
        self.setStatus(name, "done" if member["result"] is not None else "failed")

    # A result is delivered once: after a resume, the agent already has the results it received before the swarm stopped.
    def deliverResult(self, sender, receiver):
        result = str(self.members[sender]["result"])
        if not any(message["sender"] == sender and message["receiver"] == receiver and message["message"] == result for message in self.messages):
            self.communicate(sender, receiver, result)

    # Runs in its own thread. It waits for the agents it needs, works, and then reports to the leader.
    def runWorker(self, name, finished):
        member = self.getMember(name)
        try:
            if member["status"] in ("done", "failed"):
                return
            for other in member["waitsFor"]:
                finished[other].wait()
            missing = [other for other in member["waitsFor"] if self.members[other]["result"] is None]
            if missing:
                member["error"] = f"{', '.join(missing)} did not finish, so {name} could not start."
                self.setStatus(name, "failed")
            else:
                for other in member["waitsFor"]:
                    self.deliverResult(other, name)
                self.waitForStart(name)
                self.runMember(name)
            outcome = member["result"] if member["status"] == "done" else f"FAILED. {member['error']}"
            self.communicate(name, self.leader, f"{member['role']}: {outcome}")
        finally:
            finished[name].set()

    # Everything of the last run is cleared, except the plans approved in plan mode, which the agents follow when they execute.
    # A resumed run keeps it all: what is done stays done, and the agents that did not finish remember what they did.
    def prepareRun(self, resume=False):
        self.stopped, self.interruption, self.saveFailed = False, None, False
        if not resume:
            self.messages, self.summary = [], ""
        for name, member in self.members.items():
            if resume:
                self.settle(member)
            else:
                member.update(status="waiting", result=None, error="", mode=self.mode, review="", draft="", problem="", decision=None, revision=0,
                              startAt=None, started=False, resumeStart=None)
            member["wake"].clear()
            agent = member["agent"]
            if not resume:
                agent.inbox, agent.userMessages, agent.progress, agent.actions = [], [], {}, []
                if self.mode == "plan":
                    agent.approvedPlan = ""
            agent.resumed = resume
            agent.name, agent.reviewer = name, partial(self.waitForReview, name)
            agent.onConnectionLost, agent.onProgress = partial(self.waitForResume, name), self.checkpoint
        self.active = True
        self.startHeartbeat()
        self.emit("run", mode=self.mode)
        if not resume:
            for name in self.members:
                if name != self.leader:
                    self.communicate(self.leader, name, self.describe(name))
            self.getMember(self.leader)["agent"].receive("swarm", self.describe(self.leader))

    # While the swarm runs its state is also written every few seconds. A saved state that is not renewed is of a program that stopped.
    def startHeartbeat(self):
        self.heartbeat = threading.Event()
        threading.Thread(target=self.beat, args=(self.heartbeat,), daemon=True).start()

    def beat(self, stop):
        while not stop.wait(HEARTBEAT_SECONDS):
            self.checkpoint()

    # The run is over. A swarm that ended (or that the user stopped) has nothing left to continue, so its saved state is deleted.
    # One that was cut short by an error keeps it, marked as interrupted, so the next start finds it at once.
    def closeRun(self, ended):
        self.heartbeat.set()
        try:
            with self.changed:
                state = self.describeState()
        except Exception:
            state = None
        with STATE_LOCK:
            self.active = False
            try:
                if ended or self.stopped:
                    clearSwarmState(self.id)
                elif state:
                    saveSwarmState(self.id, {**state, "state": "interrupted"})
            except OSError:
                traceback.print_exc()

    def runInThread(self, work, name):
        try:
            work(name)
        finally:
            with self.changed:
                self.changed.notify_all()

    # waits is {agent: [agents it waits for]} for a group of agents. Each one starts when the agents it waits for are done,
    # and the user is looked after (reviewStage) until all of them are finished.
    def runStage(self, waits, work):
        names = list(waits)
        threads = {name: threading.Thread(target=self.runInThread, args=(work, name), daemon=True) for name in names}
        for thread in threads.values():
            thread.start()
        self.reviewStage(names, threads, waits)
        for thread in threads.values():
            thread.join(timeout=STOP_TIMEOUT if self.stopped else None)

    # Plan mode: all the agents, the leader too, write their plans at the same time. It returns the summary plan, or None if a plan was not approved.
    # If the user approved every plan on its own and never saw a summary, the leader writes one now.
    def runPlanning(self):
        self.runStage({name: [] for name in self.members}, self.runMember)
        if any(member["status"] != "done" for member in self.members.values()):
            return None
        if not self.summary:
            self.getMember(self.leader)["agent"].notifyUser(f"[{self.leader}] Summary of the approved plans:\n{self.writeSummary('')}")
        return self.summary if len(self.members) > 1 else self.members[self.leader]["result"]

    # Execute mode: each agent starts when the agents it waits for are done, then the leader works last.
    def runExecution(self):
        workers = {name: member["waitsFor"] for name, member in self.members.items() if name != self.leader}
        finished = {name: threading.Event() for name in workers}
        self.runStage(workers, partial(self.runWorker, finished=finished))
        if self.stopped:
            return None
        self.runStage({self.leader: []}, self.runLeader)
        return self.members[self.leader]["result"]

    # With resume=True the swarm goes on where it was interrupted (see resume) instead of starting again.
    def run(self, resume=False):
        self.getStages()
        self.checkGpus()
        self.prepareRun(resume)
        result, ended = None, False
        try:
            result = self.runPlanning() if self.mode == "plan" else self.runExecution()
            ended = True
            return result
        finally:
            for member in self.members.values():
                agent = member["agent"]
                agent.reviewer = agent.onConnectionLost = agent.onProgress = None
                agent.resumed = False
            self.closeRun(ended)
            self.emit("finished", mode=self.mode, ok=result is not None)
