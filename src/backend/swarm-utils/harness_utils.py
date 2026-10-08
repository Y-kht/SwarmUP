import html
import http.client
import json
import os
import re
import smtplib
import socket
import threading
import urllib.request
import xml.etree.ElementTree as ElementTree
import zlib
from pathlib import Path


# We need to define a set of tools used in each harness module.
# A harness module is then a list of functions/classes that
# are necessary for a given task.
# An agent is any object with an input(prompt) method that returns the model's answer as text.
# Every loop writes a draft, verifies it, asks the user, and only then acts (send, book, save...).
# The major prompts of the loops are in agent_prompts.py and the lists to pick from are in sources_library.py.
# This module has the tools every part needs. The loops are in base_loop.py, message_loops.py, writing_loops.py and checking_loops.py,
# the swarm in swarm_harness.py, and the information of the internet, the costs, the GPUs and the messaging apps in their own modules.

# The folder of the project, three folders above this file (src/backend/swarm-utils). The files of the agents are in agent-files, or in the
# folder of the SWARMUP_HOME environment variable (for a packaged command, or a test).
PROJECT_FOLDER = Path(__file__).resolve().parents[3]
AGENT_FILES = Path(os.environ["SWARMUP_HOME"]).expanduser() if os.environ.get("SWARMUP_HOME") else PROJECT_FOLDER / "agent-files"
AGENT_RULES = PROJECT_FOLDER / "agent-rules"
MAX_CONTEXT_ITEMS = 5

SUMMARY_LENGTH = 1500

FETCH_TIMEOUT = 20
MAX_DOWNLOAD = 5000000

USER_AGENT = "Mozilla/5.0 (compatible; HarnessForGood/1.0)"
YES_WORDS = ("yes", "y", "ok", "okay", "approve")
NO_WORDS = ("no", "n", "cancel", "stop")
USER_NAME = "User"

PHONE_PATTERN = r"\+\d{6,15}"

ABSTRACT_META_NAMES = ("citation_abstract", "dc.description", "og:description", "description")

PRICE_REFRESH_SECONDS = 3600

FETCH_ERRORS = (OSError, ValueError, ElementTree.ParseError, http.client.HTTPException, zlib.error)
# Public servers that are asked for a connection (nothing is sent) to know if the internet works at all.
CONNECTION_HOSTS = (("1.1.1.1", 443), ("8.8.8.8", 443), ("9.9.9.9", 443))
CONNECTION_TIMEOUT = 3

STATE_VERSION = 1

STOPPED_MESSAGE = "The user stopped the swarm before this agent finished."

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
