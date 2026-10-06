# Checks that need the internet. They are not part of the normal tests, because websites change:
# a problem here usually means that an outlet or a search changed.
# Run it with: python tests/live_checks.py outlets papers news literature models costs gpus publishers servers keys localmodel (or without names to run everything)
# The gpus check needs no internet. Run it on a Linux and on a Windows computer to see what the GPU reading finds on each.
# The localmodel check runs the smallest local model for real: it uses (and fills) the Hugging Face cache of the HF_HOME environment variable.
import imaplib
import json
import re
import smtplib
import sys
import tempfile
import time
import xml.etree.ElementTree as ElementTree
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

# The modules of SwarmUP are in the folders of src/backend. Their names have hyphens, so they are not packages: each folder goes on the path.
sys.path[:0] = [str(folder) for folder in sorted((Path(__file__).resolve().parent.parent / "src" / "backend").iterdir()) if folder.is_dir() and not folder.name.startswith(("_", "."))]
import gpu_check
import harness_utils
from harness_utils import FETCH_ERRORS, describeError, fetchUrl, readAbstract, readFeed
from internet_cache import getModelCost
from message_loops import NewsLoop
from model_clients import LocalModel
from model_support import getHubFolder
from models_library import API_KEYS, MODELS_API, MODELS_LOCAL, PRICING_PAGES, getVram
from sources_library import EMAIL_PROVIDERS, NEWS_OUTLETS, PAPER_PUBLISHERS
from writing_loops import LiteratureSurveyLoop, PAPER_SEARCHES, searchCrossref

STALE_DAYS = 120
problems = []


def report(name, passed, details=""):
    print(("PASS " if passed else "FAIL ") + name + (f" ({details})" if details else ""))
    if not passed:
        problems.append(name)


def newestDate(data):
    dates = []
    for element in ElementTree.fromstring(data).iter():
        text = (element.text or "").strip()
        if element.tag.split("}")[-1] in ("pubDate", "published", "updated", "date") and text:
            try:
                date = datetime.fromisoformat(text.replace("Z", "+00:00")) if text[:2].isdigit() else parsedate_to_datetime(text)
            except (ValueError, TypeError):
                continue
            dates.append(date if date.tzinfo else date.replace(tzinfo=timezone.utc))
    return max(dates) if dates else None


def testOutlet(entry):
    group, name, url = entry
    try:
        items = readFeed(url)
        date = newestDate(fetchUrl(url))
    except FETCH_ERRORS as error:
        return name, f"does not work: {describeError(error)}"
    if not items or not items[0]["title"]:
        return name, "has no stories"
    if date and (datetime.now(timezone.utc) - date).days > STALE_DAYS:
        return name, f"is stale, its newest story is {(datetime.now(timezone.utc) - date).days} days old"
    return name, ""


def checkOutlets():
    entries = [(group, name, url) for group, outlets in NEWS_OUTLETS.items() for name, url in outlets.items()]
    with ThreadPoolExecutor(max_workers=24) as pool:
        results = list(pool.map(testOutlet, entries))
    broken = [(name, why) for name, why in results if why]
    for name, why in broken:
        print(f"  {name} {why}. Remove it from sources_library.py or fix its address.")
    report(f"news outlets: {len(results) - len(broken)} of {len(results)} work", len(broken) <= len(results) // 10, f"{len(broken)} to fix")


def checkPapers(subject="graph neural networks"):
    for name, search in PAPER_SEARCHES.items():
        try:
            papers = search(subject)
        except FETCH_ERRORS as error:
            report(f"{name} search", False, describeError(error))
            continue
        clean = all(paper["title"] and paper["link"].startswith("http") for paper in papers)
        report(f"{name} search found {len(papers)} papers", len(papers) > 0 and clean)
    for link in ("https://arxiv.org/abs/1706.03762", "https://www.nature.com/articles/s43586-024-00294-7"):
        try:
            report(f"publisher page {link.split('/')[2]} gives an abstract", len(readAbstract(fetchUrl(link))) > 400)
        except FETCH_ERRORS as error:
            report(f"publisher page {link.split('/')[2]}", False, describeError(error))


class CitingAgent:
    # A pretend model. It reads the real prompt and cites the first papers or stories it was given.
    def __init__(self, invent=False):
        self.prompts, self.invent = [], invent

    def input(self, prompt):
        self.prompts.append(prompt)
        found = re.findall(r"^- (.*?): .*?\((https?://[^\s)]+)\)$", prompt, re.MULTILINE)
        lines = "\n".join(f"{title} - {link}" for title, link in found[:5])
        return lines + ("\nhttps://example.com/invented" if self.invent and len(self.prompts) == 1 else "")


def silent(loop, answers):
    loop.said = []
    loop.notifyUser = loop.said.append
    loop.askUser = lambda question: answers.pop(0)
    loop.askLogin = lambda host: loop.said.append(f"asked for an account on {host}")
    return loop


def checkNews():
    harness_utils.AGENT_FILES = Path(tempfile.mkdtemp())
    outlets = ["BBC World", "Al Jazeera English", "https://www.theguardian.com/world", "https://example.invalid/feed", "file:///etc/passwd", "NotAnOutlet"]
    agent = CitingAgent()
    loop = silent(NewsLoop(agent, outlets=outlets, maxWords=400), ["yes"])
    briefing = loop.run()
    said = "\n".join(loop.said)
    report("news: a briefing was written and saved", briefing is not None and bool(harness_utils.loadContext("news_contexts.json")))
    report("news: the web page of an outlet led to its feed", "Read " in said and "Skipping https://www.theguardian.com/world" not in said)
    report("news: unreachable, local and unknown outlets were skipped with a reason", all(text in said for text in ("Skipping https://example.invalid/feed", "Skipping file:///etc/passwd", "Skipping NotAnOutlet")))
    print("  " + [line for line in loop.said if line.startswith("Read ")][0])


def checkLiterature(subject="graph neural networks"):
    harness_utils.AGENT_FILES = Path(tempfile.mkdtemp())
    agent = CitingAgent(invent=True)
    loop = silent(LiteratureSurveyLoop(agent, subject, 400), ["yes"])
    survey = loop.run()
    said = "\n".join(loop.said)
    report(f"literature: {len(loop.papers)} papers from {len(PAPER_SEARCHES)} sources", len(loop.papers) >= 20 and not any("Skipping the search" in line for line in loop.said))
    report("literature: publisher pages gave abstracts", sum(len(paper["summary"]) >= 400 for paper in loop.papers) >= 15, f"{sum(len(paper['summary']) >= 400 for paper in loop.papers)} of {len(loop.papers)} with 400+ characters")
    report("literature: an invented citation was caught", len(agent.prompts) > 1 and "not in the paper list" in agent.prompts[1])
    report("literature: the survey was saved without any account details", survey is not None and "password" not in (harness_utils.AGENT_FILES / "literature_contexts.json").read_text(encoding="utf-8").lower())
    refused = [line for line in loop.said if "asked for an account" in line]
    print(f"  publishers that asked for an account (skipped here): {refused}")


def checkModels():
    def exists(name):
        try:
            fetchUrl(f"https://huggingface.co/api/models/{name}")
        except FETCH_ERRORS as error:
            return name, describeError(error)
        return name, ""
    names = [name for family in MODELS_LOCAL.values() for name in family]
    with ThreadPoolExecutor(max_workers=8) as pool:
        missing = [(name, why) for name, why in pool.map(exists, names) if why]
    for name, why in missing:
        print(f"  {name}: {why}. Remove it from models_library.py or fix its name.")
    report(f"local models: {len(names) - len(missing)} of {len(names)} exist on Hugging Face", not missing)


def checkCosts():
    names = [(provider, name) for provider, models in MODELS_API.items() for name in models]
    costs = [(name, getModelCost(provider, name)) for provider, name in names]
    missing = [(name, cost["error"]) for name, cost in costs if "error" in cost]
    for name, why in missing:
        print(f"  {name}: {why}")
    report(f"model costs: {len(names) - len(missing)} of {len(names)} API models have a price", not missing)
    for name, cost in costs[:3]:
        print(f"  {name}: {cost.get('input')} in, {cost.get('output')} out ({cost.get('unit')})")
    for provider, page in PRICING_PAGES.items():
        try:
            fetchUrl(page)
            report(f"official pricing page of {provider} opens", True)
        except FETCH_ERRORS as error:
            report(f"official pricing page of {provider} opens", False, describeError(error))


def checkGpus():
    gpus = gpu_check.queryGpus()
    for gpu in gpus:
        print(f"  {gpu['name']}: {gpu['total']} GB in total, {gpu['free']} GB free now")
    status = gpu_check.checkVram(0.0, gpus)
    sizes = [size for family in MODELS_LOCAL.values() for size in family.values()]
    print(f"  all the GPUs: {status['total']} GB in total, {status['free']} GB free, {status['used']} GB used by other jobs")
    print(f"  listed local models that fit in the total: {sum(getVram(size) <= status['total'] for size in sizes)} of {len(sizes)}, in the free memory: {sum(getVram(size) <= status['free'] for size in sizes)}")
    report(f"gpus: {len(gpus)} GPU(s) found", bool(gpus), "" if gpus else gpu_check.NO_GPU_MESSAGE)


# Every publisher of the list must still exist at Crossref and have papers. Crossref asks for a gentle pace, so they are asked one after the other,
# and one that is refused for asking too much is asked again a few seconds later.
def checkPublishers():
    def test(entry):
        name, number = entry
        for attempt in range(2):
            try:
                registrant = json.loads(fetchUrl(f"https://api.crossref.org/members/{number}"))["message"]["primary-name"]
                papers = searchCrossref("machine learning", number)
            except FETCH_ERRORS as error:
                if "too many requests" in describeError(error) and not attempt:
                    time.sleep(8)
                    continue
                return name, f"does not work: {describeError(error)}"
            return name, "" if papers else f"gave no papers (Crossref calls it {registrant})"
    results = []
    for entry in PAPER_PUBLISHERS.items():
        results.append(test(entry))
        time.sleep(0.3)
    for name, why in results:
        if why:
            print(f"  {name}: {why}")
    report(f"publishers: {sum(not why for name, why in results)} of {len(results)} give papers", not any(why for name, why in results))


# The mail servers of the providers must accept the connections of the program: STARTTLS on port 587 (send) and SSL on port 993 (read).
def checkServers():
    def test(entry):
        provider, (smtp, imap, note) = entry
        try:
            with smtplib.SMTP(smtp, 587, timeout=15) as server:
                server.starttls()
            with imaplib.IMAP4_SSL(imap, timeout=15):
                pass
        except (OSError, smtplib.SMTPException, imaplib.IMAP4.error) as error:
            return provider, f"{smtp} or {imap} does not work: {error}"
        return provider, ""
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(test, EMAIL_PROVIDERS.items()))
    for provider, why in results:
        if why:
            print(f"  {provider}: {why}")
    report(f"mail servers: {sum(not why for provider, why in results)} of {len(results)} providers accept the connections", not any(why for provider, why in results))


def checkKeys():
    for provider, details in API_KEYS.items():
        try:
            fetchUrl(details["page"])
            report(f"the page to create the key of {details['company']} opens", True)
        except FETCH_ERRORS as error:
            report(f"the page to create the key of {details['company']} opens", False, describeError(error))


# Runs the smallest local model of the library for real: it needs torch, transformers and accelerate, a GPU, and downloads the model the first time.
def checkLocalModel():
    name = min((size, name) for family in MODELS_LOCAL.values() for name, size in family.items() if name.startswith("Qwen/Qwen3.5"))[1]
    print(f"  model: {name}, cache: {getHubFolder()}")
    model = LocalModel(name, report=lambda message: print(f"  {message}"))
    answer = model.input("Reply with exactly one word: pong")
    print(f"  answer: {answer!r}, tokens: {model.usage}")
    report(f"local model: {name} answered", "pong" in answer.lower())
    model.unload()


CHECKS = {"outlets": checkOutlets, "papers": checkPapers, "news": checkNews, "literature": checkLiterature, "models": checkModels, "costs": checkCosts,
          "gpus": checkGpus, "publishers": checkPublishers, "servers": checkServers, "keys": checkKeys, "localmodel": checkLocalModel}


if __name__ == "__main__":
    for name in sys.argv[1:] or CHECKS:
        print(f"=== {name} ===")
        CHECKS[name]()
    print("\nEverything works." if not problems else f"\nProblems: {problems}")
    sys.exit(1 if problems else 0)
