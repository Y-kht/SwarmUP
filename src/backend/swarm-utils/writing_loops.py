# The loops that write a text: the writer, the literature reviewer with its search of papers, the document formatter,
# and the leader that writes the final report of a swarm it built.
import html
import json
import re
import urllib.request
from collections import Counter
from functools import partial
from pathlib import Path

import agent_prompts as prompts
from base_loop import Loop
from harness_utils import (FETCH_ERRORS, SUMMARY_LENGTH, addToContext, checkLength, describeError, fetchUrl, formatItems, getWords, readAbstract, readFeed,
                           retrieveContext, stripTags)
from internet_cache import remember


PAPERS_PER_SEARCH = 10

MIN_ABSTRACT_LENGTH = 400

PUBLISHERS_REFRESH_SECONDS = 7 * 24 * 3600


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
# The answer is kept a week (see remember), and given from the last connection without internet.
def findPublishers(name):
    def fetch():
        query = urllib.parse.urlencode({"query": name.strip(), "rows": 8})
        items = json.loads(fetchUrl(f"https://api.crossref.org/members?{query}")).get("message", {}).get("items", [])
        found = [{"name": item["primary-name"], "id": item["id"], "papers": item.get("counts", {}).get("total-dois", 0)}
                 for item in items if item.get("primary-name") and item.get("id")]
        return sorted(found, key=lambda publisher: -publisher["papers"])
    return remember(f"crossref-publishers-{name.strip().lower()}", fetch, PUBLISHERS_REFRESH_SECONDS)


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
# Leader harness. The leader of a swarm that it built itself (see leader_utils.py) has no task besides leading: it follows the agents,
# proposes changes of the swarm to the user (LeaderManager reads them in what it writes), and works last like every leader: it writes the
# final report of the mission from the results the agents sent it. The report is saved in the folder of the mission once the user approves it.
# ==============
class LeaderLoop(Loop):
    def __init__(self, agent, mission, numberOfLoops=5):
        super().__init__(agent, numberOfLoops)
        self.mission = mission

    def describeTask(self):
        return "It leads the swarm it built: it follows the work of the agents, proposes changes to you, and writes the final report of the mission from their results."

    def run(self):
        report = self.reviewLoop(prompts.LEADER_REPORT_PROMPT.format(mission=self.mission), lambda draft: "" if draft.strip() else "Write the report.", key="report")
        if report is None:
            return None
        path = self.saveResult("report", report)
        if path:
            self.notifyUser(f"The report is saved in {path}.")
        return report
