import base64
import gzip
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
from datetime import datetime, timedelta
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import harness_utils
from harness_utils import (NO_GPU_MESSAGE, USER_NAME, AuthorLoop, CalendarLoop, CoderLoop, ConnectionLost, DocumentFormatLoop, EmailLoop, LiteratureSurveyLoop,
                           Loop, MathCheckLoop, MessagingError, NewsLoop, Swarm, addToContext, checkMessenger, describeError, fetchUrl, findTelegramChats,
                           getFromContext, loadContext, checkVram, readAbstract, readFeed, readGpus, retrieveContext, sendMessage, splitMessage)
from models_library import getModelInfo
from sources_library import MESSAGING_APPS

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>
<item><title>First &amp; best</title><link>http://x.test/1</link><description><![CDATA[<p>Hello <b>world</b> &amp; all</p>]]></description></item>
<item><title>Second</title><link>http://x.test/2</link><description>Plain</description></item></channel></rss>"""
ATOM = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><title>An
 Atom   paper</title><link href="http://a.test/abs/1" rel="alternate"/><link href="http://a.test/pdf/1" rel="related"/>
<summary>Atom summary</summary></entry></feed>"""
PAPER_PAGE = ('<html><head><meta name="description" content="Short site text"/>'
              '<meta name="citation_abstract" content="A real abstract with Erd&amp;#336;s inside, long enough to win."/></head></html>')
SCHOLAR_PAGE = """<html><body>
<div class="gs_r gs_or gs_scl"><div class="gs_ri"><h3 class="gs_rt" ontouchstart="x"><a id="a1" href="https://www.nature.com/articles/s1" data-clk="hl=en&amp;sa=T"><b>Graph neural networks</b> in nature</a></h3><div class="gs_a"><a href="/citations?user=1">G Corso</a>, H Stark… - Nature Reviews …, 2024 - nature.com</div><div class="gs_rs"><b>Graphs</b> are flexible objects<br>
that represent entities.</div><div class="gs_fl">Cited by 10</div></div></div>
<div class="gs_r"><div class="gs_ri"><h3 class="gs_rt"><span class="gs_ctg2">[PDF]</span> <a href="https://arxiv.org/pdf/1.pdf">A &amp; B survey</a></h3><div class="gs_a">Z Wu - arXiv, 2020 - arxiv.org</div><div class="gs_rs">Snippet two.</div></div></div>
<div class="gs_r"><div class="gs_ri"><h3 class="gs_rt"><span class="gs_ctu">[CITATION]</span><span>Only a citation</span></h3><div class="gs_a">X - 1999</div></div></div>
</body></html>"""


class FakeAgent:
    def __init__(self, replies=()):
        self.replies = list(replies)
        self.prompts = []

    def input(self, prompt):
        self.prompts.append(prompt)
        return self.replies.pop(0)


# The smallest loop that executes something: it drafts what the agent is about to do, and the approved draft is its result.
class DraftLoop(Loop):
    def run(self):
        return self.reviewLoop(f"Do the task of {self.name}", lambda draft: "")


# A draft loop that must start some seconds after the agents it waits for are done, like the news briefer.
class TimedDraftLoop(DraftLoop):
    def __init__(self, agent, delay):
        super().__init__(agent)
        self.delay = delay

    def startTime(self):
        return datetime.now() + timedelta(seconds=self.delay)


class LoopTestCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        patcher = mock.patch.object(harness_utils, "AGENT_FILES", self.folder)
        patcher.start()
        self.addCleanup(patcher.stop)

    # Makes a loop run without a person. The answers are given in the order the loop asks its questions.
    def script(self, loop, answers):
        loop.said = []
        loop.notifyUser = loop.said.append
        loop.askUser = lambda question: answers.pop(0)
        return loop

    def saidText(self, loop):
        return "\n".join(loop.said)


class ContextTests(LoopTestCase):
    def testAddAndGetNestedKeys(self):
        addToContext("test.json", ["a", "b", "c"], "value")
        addToContext("test.json", ["a", "b", "d"], "other")
        self.assertEqual(getFromContext("test.json", ["a", "b", "c"]), "value")
        self.assertEqual(getFromContext("test.json", ["a", "b"]), {"c": "value", "d": "other"})

    def testMissingAndEmptyFilesAreEmpty(self):
        (self.folder / "empty.json").write_text("")
        self.assertEqual(loadContext("empty.json"), {})
        self.assertEqual(loadContext("never_written.json"), {})
        self.assertIsNone(getFromContext("empty.json", ["x", "y"]))

    def testRetrieveContextKeepsTheLastItems(self):
        self.assertEqual(retrieveContext("none.json", ["a"]), "Nothing yet.")
        for number in range(8):
            addToContext("many.json", ["items", f"k{number}"], number)
        recent = json.loads(retrieveContext("many.json", ["items"]))
        self.assertEqual(list(recent), ["k3", "k4", "k5", "k6", "k7"])

    def testUnicodeIsKeptReadable(self):
        addToContext("unicode.json", ["x"], "مرحبا")
        self.assertIn("مرحبا", (self.folder / "unicode.json").read_text(encoding="utf-8"))

    def testManyThreadsWritingNothingIsLost(self):
        def write(number):
            for item in range(20):
                addToContext("threads.json", [f"t{number}", str(item)], item)
        threads = [threading.Thread(target=write, args=(number,)) for number in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sum(len(value) for value in loadContext("threads.json").values()), 120)


class ReviewLoopTests(LoopTestCase):
    def makeLoop(self, replies, answers, numberOfLoops=5, rulesFile=None):
        loop = Loop(FakeAgent(replies), numberOfLoops, rulesFile)
        return self.script(loop, answers)

    def testApprovedDraftIsReturned(self):
        loop = self.makeLoop(["draft one"], ["yes"])
        self.assertEqual(loop.reviewLoop("task", lambda draft: ""), "draft one")
        self.assertIn("The automatic checks passed.", loop.said)

    def testRejectionAndEmptyReplyReturnNone(self):
        for answer in ("no", "n", "  ", ""):
            loop = self.makeLoop(["draft"], [answer])
            self.assertIsNone(loop.reviewLoop("task", lambda draft: ""), answer)

    def testWordsThatContainYesAreNotYes(self):
        loop = self.makeLoop(["draft", "better draft"], ["yesterday was better, shorter please", "y"])
        self.assertEqual(loop.reviewLoop("task", lambda draft: ""), "better draft")

    def testChangeRequestSendsPreviousDraftAndRequest(self):
        loop = self.makeLoop(["first", "second"], ["make it shorter", "yes"])
        self.assertEqual(loop.reviewLoop("write a text", lambda draft: ""), "second")
        self.assertIn("Your previous draft:\nfirst", loop.agent.prompts[1])
        self.assertIn("make it shorter", loop.agent.prompts[1])
        self.assertIn("write a text", loop.agent.prompts[1])

    def testFailedChecksAreFixedWithoutBotheringTheUser(self):
        asked = []
        loop = self.makeLoop(["bad", "good"], ["yes"])
        loop.askUser = lambda question: asked.append(question) or "yes"
        verify = lambda draft: "too bad" if draft == "bad" else ""
        self.assertEqual(loop.reviewLoop("task", verify), "good")
        self.assertEqual(len(asked), 1)
        self.assertIn("too bad", loop.agent.prompts[1])

    def testLastDraftWithAProblemIsStillShownWithAWarning(self):
        loop = self.makeLoop(["bad", "still bad"], ["yes"], numberOfLoops=2)
        self.assertEqual(loop.reviewLoop("task", lambda draft: "a problem"), "still bad")
        self.assertIn("Warning, the automatic checks found a problem: a problem", loop.said)

    def testStopsAfterTheMaximumNumberOfDrafts(self):
        loop = self.makeLoop(["1", "2", "3"], ["change", "change", "change"], numberOfLoops=3)
        self.assertIsNone(loop.reviewLoop("task", lambda draft: ""))
        self.assertEqual(len(loop.agent.prompts), 3)
        self.assertIn("Stopped after 3 drafts.", loop.said)

    def testRulesAndMessagesFromOtherAgentsAreInThePrompt(self):
        loop = self.makeLoop(["draft"], ["yes"], rulesFile="EMAIL_RULES.md")
        loop.receive("Writer", "here is my text")
        loop.reviewLoop("the task", lambda draft: "")
        prompt = loop.agent.prompts[0]
        self.assertTrue(prompt.startswith("Follow these rules strictly:"))
        self.assertIn("Writer: here is my text", prompt)
        self.assertTrue(prompt.endswith("the task"))

    def testUserMessagesComeAfterTheOtherAgentsAndBeforeTheTask(self):
        loop = self.makeLoop(["draft"], ["yes"], rulesFile="EMAIL_RULES.md")
        loop.receive("Writer", "here is my text")
        loop.receiveFromUser("Use {a} formal tone")
        loop.reviewLoop("the task", lambda draft: "")
        prompt = loop.agent.prompts[0]
        self.assertTrue(prompt.startswith("Follow these rules strictly:"))
        self.assertLess(prompt.index("Writer: here is my text"), prompt.index("- Use {a} formal tone"))
        self.assertLess(prompt.index("- Use {a} formal tone"), prompt.index("the task"))
        self.assertTrue(prompt.endswith("the task"))
        self.assertIn("exactly the format your task asks for", prompt)

    def testMessagesAlreadyInThePromptDoNotMakeTheAgentWriteAgain(self):
        loop = self.makeLoop(["draft"], ["yes"])
        loop.receiveFromUser("Be brief")
        self.assertEqual(loop.reviewLoop("task", lambda draft: ""), "draft")
        self.assertEqual(len(loop.agent.prompts), 1)

    # Pretends the user typed a message while the agent was busy with its first answer.
    def sendMessageDuringFirstAnswer(self, loop, message):
        answer = loop.agent.input
        def inputWithMessage(prompt):
            if not loop.agent.prompts:
                loop.receiveFromUser(message)
            return answer(prompt)
        loop.agent.input = inputWithMessage

    def testAMessageThatArrivesWhileWritingMakesTheAgentWriteAgain(self):
        loop = self.makeLoop(["first", "second"], ["yes"])
        self.sendMessageDuringFirstAnswer(loop, "Mention the budget")
        self.assertEqual(loop.reviewLoop("task", lambda draft: ""), "second")
        self.assertNotIn("budget", loop.agent.prompts[0])
        self.assertIn("- Mention the budget", loop.agent.prompts[1])
        self.assertIn("Your previous draft:\nfirst", loop.agent.prompts[1])
        self.assertIn("The user sent new messages", loop.agent.prompts[1])
        self.assertEqual(len([said for said in loop.said if said.startswith("[")]), 1)

    def testAMessageDuringTheLastDraftIsShownAsAWarning(self):
        loop = self.makeLoop(["only"], ["yes"], numberOfLoops=1)
        self.sendMessageDuringFirstAnswer(loop, "Mention the budget")
        self.assertEqual(loop.reviewLoop("task", lambda draft: ""), "only")
        self.assertTrue(any("The user sent new messages" in said for said in loop.said))

    def testWithoutAskTheUserIsNeverInvolved(self):
        loop = self.makeLoop(["draft"], [])
        self.assertEqual(loop.reviewLoop("task", lambda draft: "", ask=False), "draft")
        loop = self.makeLoop(["bad", "bad"], [], numberOfLoops=2)
        self.assertIsNone(loop.reviewLoop("task", lambda draft: "a problem", ask=False))
        self.assertEqual(loop.said, [])

    def testTheReviewerAnswersInsteadOfTheConsole(self):
        loop = self.makeLoop(["draft"], [], numberOfLoops=1)
        loop.describe = lambda draft: f"shown {draft}"
        seen = []
        loop.reviewer = lambda shown, problem: seen.append((shown, problem)) or "yes"
        self.assertEqual(loop.reviewLoop("task", lambda draft: "a problem"), "draft")
        self.assertEqual(seen, [("shown draft", "a problem")])
        self.assertEqual(loop.said, [])

    def testAPlanIsWrittenAgainUntilTheUserApprovesIt(self):
        loop = self.makeLoop(["step one", "step two"], ["make it shorter", "yes"])
        loop.describe = lambda draft: f"DESCRIBED {draft}"
        self.assertEqual(loop.makePlan("send the report"), "step two")
        self.assertEqual(loop.approvedPlan, "step two")
        self.assertIn("Write a plan for your task: send the report", loop.agent.prompts[0])
        self.assertIn("make it shorter", loop.agent.prompts[1])
        self.assertIn("[Loop]\nstep one", loop.said)
        self.assertNotIn("DESCRIBED", self.saidText(loop))
        self.assertFalse(loop.planning)

    def testAPlanThatIsTooLongOrEmptyIsFixedWithoutBotheringTheUser(self):
        loop = self.makeLoop(["word " * 200, "  ", "a short plan"], ["yes"])
        self.assertEqual(loop.makePlan("task"), "a short plan")
        self.assertIn("limit is 150", loop.agent.prompts[1])
        self.assertIn("Write the plan.", loop.agent.prompts[2])
        self.assertEqual(len([said for said in loop.said if said.startswith("[")]), 1)

    def testARejectedPlanIsForgotten(self):
        loop = self.makeLoop(["plan one", "plan two"], ["yes", "no"])
        loop.makePlan("task")
        self.assertEqual(loop.approvedPlan, "plan one")
        self.assertIsNone(loop.makePlan("task"))
        self.assertEqual(loop.approvedPlan, "")

    def testTheApprovedPlanIsInEveryPromptJustBeforeTheTask(self):
        loop = self.makeLoop(["the plan", "a draft"], ["yes", "yes"])
        loop.makePlan("task")
        loop.receiveFromUser("Be brief")
        loop.reviewLoop("the task", lambda draft: "")
        prompt = loop.agent.prompts[1]
        self.assertLess(prompt.index("- Be brief"), prompt.index("Your plan, approved by the user"))
        self.assertLess(prompt.index("the plan"), prompt.index("the task"))
        self.assertTrue(prompt.endswith("the task"))

    def testSettingsComeFromTheEnvironmentOrAreAskedOnce(self):
        loop = Loop(FakeAgent())
        secrets = ["pw-typed"]
        loop.askSecret = lambda question: secrets.pop(0)
        with mock.patch.dict(os.environ, {"MY_SETTING": "from-env"}):
            self.assertEqual(loop.getSetting("MY_SETTING"), "from-env")
        with mock.patch.dict(os.environ):
            os.environ.pop("MY_PASSWORD", None)
            self.assertEqual(loop.getSetting("MY_PASSWORD", secret=True), "pw-typed")
            self.assertEqual(loop.getSetting("MY_PASSWORD", secret=True), "pw-typed")


# A small website to test the downloads against. do_GET and log_message are the names the standard library requires.
class FakeWebsite(BaseHTTPRequestHandler):
    def log_message(self, *arguments):
        pass

    def send(self, code, body, headers=()):
        self.send_response(code)
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        token = "Basic " + base64.b64encode(b"user:pass").decode()
        routes = {"/text": lambda: self.send(200, b"hello"), "/gzip": lambda: self.send(200, gzip.compress(RSS.encode())),
                  "/feed": lambda: self.send(200, RSS.encode()), "/atom": lambda: self.send(200, ATOM.encode()),
                  "/forbidden": lambda: self.send(403, b"no"), "/missing": lambda: self.send(404, b"no"),
                  "/redirect": lambda: self.send(302, b"", [("Location", "/text")]),
                  "/page": lambda: self.send(200, b'<html><head><link rel="alternate" type="application/rss+xml" href="/feed"></head></html>'),
                  "/selfpage": lambda: self.send(200, b'<html><head><link rel="alternate" type="application/atom+xml" href="/selfpage"></head></html>'),
                  "/plainpage": lambda: self.send(200, b"<html><body>No feed here</body></html>"),
                  "/paper": lambda: self.send(200, PAPER_PAGE.encode()), "/pdf": lambda: self.send(200, b"%PDF-1.4 binary"),
                  "/auth": lambda: self.send(200, b"secret") if self.headers.get("Authorization") == token
                  else self.send(401, b"no", [("WWW-Authenticate", 'Basic realm="x"')])}
        routes.get(self.path, lambda: self.send(404, b"no"))()


class WebServerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["no_proxy"] = "127.0.0.1,localhost"
        os.environ["NO_PROXY"] = "127.0.0.1,localhost"
        cls.server = HTTPServer(("127.0.0.1", 0), FakeWebsite)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()


class FetchTests(WebServerTestCase):
    def testFetchesText(self):
        self.assertEqual(fetchUrl(self.base + "/text"), b"hello")

    def testFollowsRedirects(self):
        self.assertEqual(fetchUrl(self.base + "/redirect"), b"hello")

    def testCompressedDataIsUnpackedEvenIfNotAskedFor(self):
        self.assertTrue(fetchUrl(self.base + "/gzip").startswith(b"<?xml"))

    def testErrorsAreExplainedInPlainWords(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            fetchUrl(self.base + "/missing")
        self.assertIn("error 404", describeError(caught.exception))
        with self.assertRaises(OSError) as caught:
            fetchUrl("http://127.0.0.1:1/")
        self.assertIn("could not be reached", describeError(caught.exception))
        self.assertIn("too many requests", describeError(urllib.error.HTTPError("u", 429, "x", {}, None)))
        self.assertIn("too long", describeError(TimeoutError()))

    def testOnlyWebAddressesAreAllowed(self):
        for address in ("file:///etc/passwd", "ftp://x.test/a", "javascript:alert(1)", "BBC Wrld", "", "http://"):
            with self.assertRaises(ValueError, msg=address):
                fetchUrl(address)

    def testAccountsAreOnlySentOverHttps(self):
        with self.assertRaises(ValueError):
            fetchUrl("http://example.com/auth", ("user", "pass"))

    def testBasicAuthIsUsedOnlyWhenAsked(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            fetchUrl(self.base + "/auth")
        self.assertEqual(caught.exception.code, 401)
        self.assertEqual(fetchUrl(self.base + "/auth", ("user", "pass")), b"secret")
        with self.assertRaises(urllib.error.HTTPError):
            fetchUrl(self.base + "/auth", ("user", "wrong"))


class FeedTests(WebServerTestCase):
    def testReadsRss(self):
        items = readFeed(self.base + "/feed")
        self.assertEqual([item["title"] for item in items], ["First & best", "Second"])
        self.assertEqual(items[0]["summary"], "Hello world & all")
        self.assertEqual(items[0]["link"], "http://x.test/1")

    def testReadsAtomLikeArxiv(self):
        items = readFeed(self.base + "/atom")
        self.assertEqual(items, [{"title": "An Atom paper", "summary": "Atom summary", "link": "http://a.test/abs/1"}])

    def testCompressedFeedIsRead(self):
        self.assertEqual(len(readFeed(self.base + "/gzip")), 2)

    def testLimitIsRespected(self):
        self.assertEqual(len(readFeed(self.base + "/feed", limit=1)), 1)

    def testFindsTheFeedWhenGivenTheWebPageOfTheOutlet(self):
        self.assertEqual(len(readFeed(self.base + "/page")), 2)

    def testPageThatPointsToItselfDoesNotLoopForever(self):
        with self.assertRaises(Exception) as caught:
            readFeed(self.base + "/selfpage")
        self.assertIn("not an RSS or Atom feed", describeError(caught.exception))

    def testPageWithoutStoriesGivesAnEmptyListForTheLoopToReport(self):
        self.assertEqual(readFeed(self.base + "/plainpage"), [])

    def testNotXmlAtAllIsAClearError(self):
        with self.assertRaises(Exception) as caught:
            readFeed(self.base + "/text")
        self.assertIn("not an RSS or Atom feed", describeError(caught.exception))


class AbstractTests(WebServerTestCase):
    def testTheLongestDescriptionIsTheAbstractAndEntitiesAreDecoded(self):
        abstract = readAbstract(fetchUrl(self.base + "/paper"))
        self.assertTrue(abstract.startswith("A real abstract"))
        self.assertIn("ErdŐs", abstract)
        self.assertNotIn("&#", abstract)

    def testPdfsAndEmptyPagesGiveNothing(self):
        self.assertEqual(readAbstract(fetchUrl(self.base + "/pdf")), "")
        self.assertEqual(readAbstract(b"<html></html>"), "")


class EmailTests(LoopTestCase):
    settings = {"EMAIL_PASSWORD": "pw", "EMAIL_SMTP_SERVER": "smtp.test", "EMAIL_IMAP_SERVER": "imap.test"}

    # Replaces the mail servers by pretend ones. emails maps the id of an email in the inbox to its raw bytes.
    def useMail(self, emails=None):
        patchers = [mock.patch.dict(os.environ, self.settings), mock.patch.object(harness_utils.smtplib, "SMTP"),
                    mock.patch.object(harness_utils.imaplib, "IMAP4_SSL")]
        started = [patcher.start() for patcher in patchers]
        for patcher in patchers:
            self.addCleanup(patcher.stop)
        self.smtp, self.server = started[1], started[1].return_value.__enter__.return_value
        self.inbox = started[2].return_value.__enter__.return_value
        self.inbox.search.return_value = ("OK", [b" ".join(emails or {})])
        self.inbox.fetch.side_effect = lambda emailId, parts: ("OK", [(b"x", (emails or {})[emailId])])

    def makeLoop(self, replies, answers, receiver="sara@example.com"):
        return self.script(EmailLoop(FakeAgent(replies), "me@example.com", receiver, "Meeting", "Ask to move the meeting"), answers)

    def rawEmail(self, subject, body):
        message = EmailMessage()
        message["Subject"], message["From"] = subject, "sara@example.com"
        message.set_content(body)
        return message.as_bytes()

    def testInvalidAddressIsRefusedBeforeAnythingElse(self):
        self.useMail()
        loop = self.makeLoop([], [], receiver="not-an-address")
        self.assertIsNone(loop.run())
        self.assertIn("not a valid email address", self.saidText(loop))
        self.smtp.assert_not_called()

    def testApprovedEmailIsSentAndRemembered(self):
        self.useMail()
        loop = self.makeLoop(["Hello Sara, can we move it?", "OK"], ["yes"])
        self.assertEqual(loop.run(), "Hello Sara, can we move it?")
        self.smtp.assert_called_with("smtp.test", 587)
        self.server.starttls.assert_called_once()
        self.server.login.assert_called_with("me@example.com", "pw")
        message = self.server.send_message.call_args[0][0]
        self.assertEqual((message["From"], message["To"], message["Subject"]), ("me@example.com", "sara@example.com", "Meeting"))
        self.assertIn("Hello Sara", message.get_content())
        self.assertEqual(getFromContext("email_contexts.json", ["me@example.com", "sara@example.com", "Meeting"]), "Hello Sara, can we move it?")

    def testRejectedEmailIsNeverSent(self):
        self.useMail()
        loop = self.makeLoop(["Draft", "OK"], ["no"])
        self.assertIsNone(loop.run())
        self.smtp.assert_not_called()
        self.assertEqual(loadContext("email_contexts.json"), {})
        self.assertIn("The email was not sent.", loop.said)

    def testRuleBreakingDraftIsRewrittenBeforeTheUserSeesIt(self):
        self.useMail()
        loop = self.makeLoop(["Rude draft", "Rule 3 is broken: offensive", "Polite draft", "OK"], ["yes"])
        self.assertEqual(loop.run(), "Polite draft")
        self.assertIn("Rule 3 is broken", loop.agent.prompts[2])

    def testFailureToSendIsNotRecordedAsSent(self):
        self.useMail()
        self.server.login.side_effect = OSError("refused")
        loop = self.makeLoop(["Draft", "OK"], ["yes"])
        with self.assertRaises(OSError):
            loop.run()
        self.assertEqual(loadContext("email_contexts.json"), {})

    def testLatestMatchingEmailIsReadAndTheInboxIsNotChanged(self):
        self.useMail({b"1": self.rawEmail("Meeting", "old"), b"2": self.rawEmail("Re: Meeting", "Can we meet at 3?"), b"3": self.rawEmail("Other topic", "unrelated")})
        loop = self.makeLoop(["Reply", "OK"], ["yes"])
        loop.run()
        self.inbox.select.assert_called_with("INBOX", readonly=True)
        self.assertIn("Can we meet at 3?", loop.agent.prompts[0])
        self.assertNotIn("unrelated", loop.agent.prompts[0])

    def testEarlierSentEmailsAreShownToTheAgent(self):
        self.useMail()
        addToContext("email_contexts.json", ["me@example.com", "sara@example.com", "Lunch"], "Hi Sara, lunch tomorrow?")
        loop = self.makeLoop(["Reply", "OK"], ["yes"])
        loop.run()
        self.assertIn("Hi Sara, lunch tomorrow?", loop.agent.prompts[0])

    def testWithoutAnImapServerANewEmailIsWritten(self):
        self.useMail()
        os.environ.pop("EMAIL_IMAP_SERVER")
        loop = self.makeLoop(["Draft", "OK"], ["yes"])
        loop.run()
        self.assertIn("None, this is a new email.", loop.agent.prompts[0])
        self.assertIn("EMAIL_IMAP_SERVER is not set", self.saidText(loop))

    def testMissingPasswordIsAskedNotCrashed(self):
        self.useMail()
        os.environ.pop("EMAIL_PASSWORD")
        os.environ.pop("EMAIL_IMAP_SERVER")
        loop = self.makeLoop(["Draft", "OK"], ["yes"])
        loop.askSecret = lambda question: "typed-password"
        loop.run()
        self.server.login.assert_called_with("me@example.com", "typed-password")


class CalendarTests(LoopTestCase):
    def event(self, date="2099-05-04", time="10:00", duration=60, subject="Lunch, with Sara; important"):
        return json.dumps({"date": date, "time": time, "duration": duration, "subject": subject})

    def makeLoop(self, replies, answers):
        return self.script(CalendarLoop(FakeAgent(replies), "Lunch with Sara"), answers)

    def testEventIsBookedAndExportedToIcs(self):
        loop = self.makeLoop([f"```json\n{self.event()}\n```"], ["yes"])
        self.assertIn("Lunch, with Sara; important", loop.run())
        self.assertEqual(getFromContext("calendar_contexts.json", ["2099-05-04", "10:00"]), {"subject": "Lunch, with Sara; important", "duration": 60})
        ics = (self.folder / "calendar_events.ics").read_bytes().decode()
        self.assertIn("DTSTART:20990504T100000\r\nDTEND:20990504T110000", ics)
        self.assertIn("SUMMARY:Lunch\\, with Sara\\; important", ics)
        self.assertTrue(ics.startswith("BEGIN:VCALENDAR\r\n") and ics.endswith("END:VCALENDAR\r\n"))
        self.assertNotIn("\r\r", ics)

    def testBadJsonAndPastTimesAreFixedByTheAgentAlone(self):
        loop = self.makeLoop(["tomorrow at ten", self.event(date="2000-01-01"), self.event()], ["yes"])
        self.assertIsNotNone(loop.run())
        self.assertIn("Reply only with JSON", loop.agent.prompts[1])
        self.assertIn("must start in the future", loop.agent.prompts[2])

    def testOverlapIsShownToTheUserWhoCanBookAnyway(self):
        addToContext("calendar_contexts.json", ["2099-05-04", "10:30"], {"subject": "Dentist", "duration": 30})
        loop = self.makeLoop([self.event()], ["yes"])
        self.assertIsNotNone(loop.run())
        self.assertIn("overlaps with 'Dentist' at 10:30", self.saidText(loop))
        self.assertEqual(len(loadContext("calendar_contexts.json")["2099-05-04"]), 2)

    def testOverlapCanBeRescheduledByTheUser(self):
        addToContext("calendar_contexts.json", ["2099-05-04", "10:30"], {"subject": "Dentist", "duration": 30})
        loop = self.makeLoop([self.event(), self.event(time="14:00")], ["move it to 2pm", "yes"])
        loop.run()
        self.assertIn("move it to 2pm", loop.agent.prompts[1])
        self.assertIn("14:00", loadContext("calendar_contexts.json")["2099-05-04"])

    def testTouchingEventsDoNotOverlap(self):
        addToContext("calendar_contexts.json", ["2099-05-04", "09:00"], {"subject": "Before", "duration": 60})
        loop = self.makeLoop([self.event()], ["yes"])
        loop.run()
        self.assertNotIn("overlaps", self.saidText(loop))

    def testRejectedEventIsNotBooked(self):
        loop = self.makeLoop([self.event()], ["no"])
        self.assertIsNone(loop.run())
        self.assertEqual(loadContext("calendar_contexts.json"), {})
        self.assertFalse((self.folder / "calendar_events.ics").exists())

    def testBookedEventsAreShownToTheAgent(self):
        addToContext("calendar_contexts.json", ["2099-05-04", "09:00"], {"subject": "Standup", "duration": 15})
        loop = self.makeLoop([self.event(time="11:00")], ["yes"])
        loop.run()
        self.assertIn("Standup", loop.agent.prompts[0])


class NewsTests(LoopTestCase):
    def fakeFeeds(self):
        original = harness_utils.readFeed
        def read(url, limit=10, discover=True):
            if not url.startswith("http"):
                return original(url)
            if url == "https://custom.test/feed":
                return [{"title": "Custom story", "summary": "custom summary", "link": "https://custom.test/1"}]
            if "bbci" in url:
                return [{"title": "BBC story", "summary": "bbc summary", "link": "https://bbc.test/1"}]
            if url == "https://empty.test/feed":
                return []
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return mock.patch.object(harness_utils, "readFeed", read)

    def makeLoop(self, replies, answers, **options):
        return self.script(NewsLoop(FakeAgent(replies), **options), answers)

    def testNamesFromTheListAndCustomAddressesAreBothUsed(self):
        loop = self.makeLoop(["Briefing https://bbc.test/1 https://custom.test/1"], ["yes"], outlets=["BBC World", "https://custom.test/feed"])
        with self.fakeFeeds():
            self.assertIsNotNone(loop.run())
        self.assertIn("BBC story", loop.agent.prompts[0])
        self.assertIn("Custom story", loop.agent.prompts[0])
        self.assertIn("Read 2 stories from 2 of 2 outlets.", loop.said)

    def testBrokenOutletsAreSkippedWithAClearMessage(self):
        loop = self.makeLoop(["Briefing"], ["yes"], outlets=["BBC World", "https://dead.test/feed", "https://empty.test/feed", "NotAnOutlet"])
        with self.fakeFeeds():
            self.assertEqual(loop.run(), "Briefing")
        said = self.saidText(loop)
        self.assertIn("Skipping https://dead.test/feed: the website answered with error 404", said)
        self.assertIn("Skipping https://empty.test/feed: it has no stories", said)
        self.assertIn("Skipping NotAnOutlet: 'NotAnOutlet' is not a web address", said)
        self.assertIn("Read 1 stories from 1 of 4 outlets.", loop.said)

    def testLocalFilesAreNeverRead(self):
        loop = self.makeLoop([], [], outlets=["file:///etc/passwd"])
        self.assertIsNone(loop.run())
        self.assertIn("not a web address", self.saidText(loop))
        self.assertIn("No headlines could be fetched", self.saidText(loop))

    def testNothingIsSavedWhenEveryOutletFailsOrTheUserSaysNo(self):
        loop = self.makeLoop([], [], outlets=["https://dead.test/feed"])
        with self.fakeFeeds():
            self.assertIsNone(loop.run())
        loop = self.makeLoop(["Briefing"], ["no"], outlets=["BBC World"])
        with self.fakeFeeds():
            self.assertIsNone(loop.run())
        self.assertEqual(loadContext("news_contexts.json"), {})

    def testBriefingIsSavedAndNotRepeatedTheSameDay(self):
        first = self.makeLoop(["Briefing one"], ["yes"], outlets=["BBC World"])
        with self.fakeFeeds():
            first.run()
        second = self.makeLoop(["Briefing two"], ["yes"], outlets=["BBC World"], topics="technology", language="Arabic")
        with self.fakeFeeds():
            second.run()
        self.assertIn("Briefing one", second.agent.prompts[0])
        self.assertIn("technology", second.agent.prompts[0])
        self.assertIn("in Arabic", second.agent.prompts[0])
        self.assertEqual(len(loadContext("news_contexts.json")), 1)

    def testBriefingOverTheWordLimitIsShortenedFirst(self):
        loop = self.makeLoop(["word " * 50, "short"], ["yes"], outlets=["BBC World"], maxWords=10)
        with self.fakeFeeds():
            self.assertEqual(loop.run(), "short")
        self.assertIn("limit is 10", loop.agent.prompts[1])

    # The sender of messages is replaced by a recorder that can fail.
    def recordSending(self, failure=None):
        sent = []
        def send(app, settings, text):
            sent.append((app, dict(settings), text))
            if failure:
                raise failure
        patcher = mock.patch.object(harness_utils, "sendMessage", send)
        patcher.start()
        self.addCleanup(patcher.stop)
        return sent

    def testTheApprovedBriefingIsSentToTheMessagingAppExactlyAsApproved(self):
        sent = self.recordSending()
        loop = self.makeLoop(["First draft", "Second draft https://bbc.test/1"], ["too long, change it", "yes"], outlets=["BBC World"], messenger="Telegram",
                             messengerSettings={"token": "123:SECRET", "chat": "42"})
        with self.fakeFeeds():
            self.assertEqual(loop.run(), "Second draft https://bbc.test/1")
        self.assertEqual(sent, [("Telegram", {"token": "123:SECRET", "chat": "42"}, "Second draft https://bbc.test/1")])
        self.assertIn("The briefing was sent to Telegram.", loop.said)
        self.assertEqual([action["text"] for action in loop.actions][-1], "Sent the news briefing to Telegram.")
        self.assertIn("also sent to Telegram", loop.describeTask())

    def testNothingIsSentBeforeTheApprovalOrWhenTheUserSaysNo(self):
        sent = self.recordSending()
        loop = self.makeLoop(["Briefing"], ["no"], outlets=["BBC World"], messenger="WhatsApp", messengerSettings={"token": "t"})
        with self.fakeFeeds():
            self.assertIsNone(loop.run())
        self.assertEqual(sent, [])

    def testWithoutAMessagingAppNothingIsSentAndTheTaskIsUnchanged(self):
        sent = self.recordSending()
        loop = self.makeLoop(["Briefing"], ["yes"], outlets=["BBC World"])
        with self.fakeFeeds():
            self.assertEqual(loop.run(), "Briefing")
        self.assertEqual(sent, [])
        self.assertNotIn("sent to", loop.describeTask())

    def testAFailedDeliveryFailsTheAgentButTheBriefingIsAlreadySaved(self):
        self.recordSending(MessagingError("Telegram refused the bot token. Copy it again from @BotFather."))
        loop = self.makeLoop(["Briefing"], ["yes"], outlets=["BBC World"], messenger="Telegram", messengerSettings={"token": "t", "chat": "1"})
        with self.fakeFeeds(), self.assertRaisesRegex(MessagingError, "refused the bot token"):
            loop.run()
        self.assertEqual(list(next(iter(loadContext("news_contexts.json").values())).values()), ["Briefing"])
        self.assertEqual(loop.progress["delivered"], "")


class AuthorTests(LoopTestCase):
    def testTooLongTextIsShortenedThenSavedAndUsedAsStyleLater(self):
        loop = self.script(AuthorLoop(FakeAgent(["one two three four five six", "one two three"]), "birds", 4), ["yes"])
        self.assertEqual(loop.run(), "one two three")
        self.assertIn("6 words but the limit is 4", loop.agent.prompts[1])
        self.assertEqual(getFromContext("author_contexts.json", ["birds"]), "one two three")
        later = self.script(AuthorLoop(FakeAgent(["fine"]), "fish", 4), ["yes"])
        later.run()
        self.assertIn("one two three", later.agent.prompts[0])

    def testRejectedTextIsNotSaved(self):
        loop = self.script(AuthorLoop(FakeAgent(["ok"]), "birds", 4), ["no"])
        self.assertIsNone(loop.run())
        self.assertEqual(loadContext("author_contexts.json"), {})


class PaperSearchTests(unittest.TestCase):
    def fetching(self, body):
        return mock.patch.object(harness_utils, "fetchUrl", lambda url, login=None: body if isinstance(body, bytes) else body.encode())

    def testGoogleScholarResultsAreParsed(self):
        with self.fetching(SCHOLAR_PAGE):
            papers = harness_utils.searchGoogleScholar("graph neural networks")
        self.assertEqual([paper["title"] for paper in papers], ["Graph neural networks in nature", "A & B survey"])
        self.assertEqual(papers[0]["link"], "https://www.nature.com/articles/s1")
        self.assertIn("Nature Reviews", papers[0]["summary"])
        self.assertIn("flexible objects that represent entities.", papers[0]["summary"])
        self.assertNotIn("<", papers[0]["summary"])

    def testGoogleScholarCaptchaIsAClearError(self):
        with self.fetching("<html><body>Our systems have detected unusual traffic. Please solve this captcha.</body></html>"):
            with self.assertRaises(ValueError) as caught:
                harness_utils.searchGoogleScholar("anything")
        self.assertIn("captcha", str(caught.exception))

    def testCrossrefResultsAreParsed(self):
        body = json.dumps({"message": {"items": [{"title": ["A <i>nice</i> paper"], "abstract": "<jats:p>The abstract.</jats:p>", "URL": "https://doi.org/10.1/a"},
                                                 {"title": ["No link"]}, {"URL": "https://doi.org/10.1/b"}]}})
        with self.fetching(body):
            self.assertEqual(harness_utils.searchCrossref("x"), [{"title": "A nice paper", "summary": "The abstract.", "link": "https://doi.org/10.1/a"}])
        with self.fetching("{}"):
            self.assertEqual(harness_utils.searchCrossref("x"), [])

    def testOpenReviewKeepsPapersAndDropsReviews(self):
        paper = {"id": "r1", "forum": "f1", "content": {"title": {"value": "A paper"}, "abstract": {"value": "Its abstract"}, "venue": {"value": "ICLR 2025"}}}
        review = {"id": "r2", "forum": "f1", "content": {"summary": {"value": "A review"}, "rating": {"value": 8}}}
        with self.fetching(json.dumps({"notes": [review, paper]})):
            papers = harness_utils.searchOpenReview("x")
        self.assertEqual(papers, [{"title": "A paper", "summary": "ICLR 2025. Its abstract", "link": "https://openreview.net/forum?id=f1"}])

    def testArxivResultsAreParsed(self):
        with self.fetching(ATOM):
            self.assertEqual(harness_utils.searchArxiv("atom paper")[0]["link"], "http://a.test/abs/1")

    def testOpenReviewIsOneOfTheSearches(self):
        self.assertEqual(list(harness_utils.PAPER_SEARCHES), ["Google Scholar", "arXiv", "Crossref", "OpenReview"])


class LiteratureTests(LoopTestCase):
    def paper(self, title, link, summary="short"):
        return {"title": title, "summary": summary, "link": link}

    def makeLoop(self, replies, answers, searches, **options):
        loop = LiteratureSurveyLoop(FakeAgent(replies), "graphs", options.pop("length", 100), searches=tuple(searches), **options)
        loop.askLogin = lambda host: loop.logins_asked.append(host) or loop.nextLogin
        loop.logins_asked, loop.nextLogin = [], None
        return self.script(loop, answers)

    def patchSearches(self, **searches):
        patcher = mock.patch.dict(harness_utils.PAPER_SEARCHES, searches, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def testSourcesAreMergedWithoutDuplicatesAndFailingOnesSkipped(self):
        def broken(subject):
            raise urllib.error.HTTPError("u", 429, "Too Many", {}, None)
        self.patchSearches(A=lambda s: [self.paper("Same Paper", "https://a.test/1")], B=lambda s: [self.paper("same  paper!", "https://b.test/1"), self.paper("Other", "https://b.test/2")], C=broken)
        loop = self.makeLoop([], [], ["A", "C", "B"])
        papers = loop.searchPapers()
        self.assertEqual([paper["link"] for paper in papers], ["https://a.test/1", "https://b.test/2"])
        self.assertIn("Skipping the search on C: the website says it received too many requests", self.saidText(loop))

    def testNothingFoundMeansNothingSaved(self):
        self.patchSearches(A=lambda s: [])
        loop = self.makeLoop([], [], ["A"])
        self.assertIsNone(loop.run())
        self.assertIn("No papers found for graphs.", loop.said)

    def testInventedLinksAreCaughtAndRealOnesAccepted(self):
        long = "x" * 500
        self.patchSearches(A=lambda s: [self.paper("Real", "https://a.test/1", long)])
        loop = self.makeLoop(["See https://invented.test/9 and https://a.test/1.", "See Real https://a.test/1."], ["yes"], ["A"])
        self.assertEqual(loop.run(), "See Real https://a.test/1.")
        self.assertIn("not in the paper list", loop.agent.prompts[1])
        saved = getFromContext("literature_contexts.json", ["graphs"])
        self.assertEqual(saved["survey"], "See Real https://a.test/1.")
        self.assertEqual(saved["sources"][0]["link"], "https://a.test/1")

    def testSurveyWithoutAnyCitationOrTooLongIsSentBack(self):
        self.patchSearches(A=lambda s: [self.paper("Real", "https://a.test/1", "x" * 500)])
        loop = self.makeLoop(["No citations at all", "https://a.test/1 " + "word " * 200, "https://a.test/1 fine"], ["yes"], ["A"])
        self.assertEqual(loop.run(), "https://a.test/1 fine")
        self.assertIn("Cite the papers", loop.agent.prompts[1])
        self.assertIn("limit is 100", loop.agent.prompts[2])

    def testOnlyShortSummariesTriggerAPublisherVisit(self):
        visited = []
        def fetch(url, login=None):
            visited.append(url)
            return PAPER_PAGE.encode()
        self.patchSearches(A=lambda s: [self.paper("Short", "https://a.test/short"), self.paper("Long", "https://a.test/long", "y" * 450)])
        loop = self.makeLoop([], [], ["A"])
        loop.papers = loop.searchPapers()
        with mock.patch.object(harness_utils, "fetchUrl", fetch):
            loop.readPublisherPages()
        self.assertEqual(visited, ["https://a.test/short"])
        self.assertTrue(loop.papers[0]["summary"].startswith("A real abstract"))
        self.assertEqual(loop.papers[1]["summary"], "y" * 450)

    def testUnreadablePublisherPagesKeepTheShortDescription(self):
        def fetch(url, login=None):
            raise urllib.error.HTTPError(url, 500, "Server Error", {}, None)
        self.patchSearches(A=lambda s: [self.paper("Short", "https://a.test/short", "kept")])
        loop = self.makeLoop([], [], ["A"])
        loop.papers = loop.searchPapers()
        with mock.patch.object(harness_utils, "fetchUrl", fetch):
            loop.readPublisherPages()
        self.assertEqual(loop.papers[0]["summary"], "kept")
        self.assertIn("error 500", self.saidText(loop))
        self.assertEqual(loop.logins_asked, [])

    def refusingPublisher(self, accepted):
        calls = []
        def fetch(url, login=None):
            calls.append((url, login))
            if url.startswith("https://doi.org/"):
                raise urllib.error.HTTPError("https://publisher.test/" + url.rsplit("/", 1)[1], 403, "Forbidden", {}, None)
            if login in accepted and url.startswith("https://publisher.test/"):
                return PAPER_PAGE.encode()
            raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)
        return fetch, calls

    def testAccountIsAskedForThePublisherThatRefusedAndUsedOnItOnlyOnce(self):
        fetch, calls = self.refusingPublisher({("me", "secret")})
        self.patchSearches(A=lambda s: [self.paper("One", "https://doi.org/10.1/one"), self.paper("Two", "https://doi.org/10.1/two")])
        loop = self.makeLoop([], [], ["A"])
        loop.nextLogin = ("me", "secret")
        loop.papers = loop.searchPapers()
        with mock.patch.object(harness_utils, "fetchUrl", fetch):
            loop.readPublisherPages()
        self.assertEqual(loop.logins_asked, ["publisher.test"])
        self.assertTrue(all(paper["summary"].startswith("A real abstract") for paper in loop.papers))
        self.assertIn(("https://publisher.test/one", ("me", "secret")), calls)
        self.assertTrue(all(login is None for url, login in calls if url.startswith("https://doi.org/")))

    def testSkippedAccountIsNotAskedAgain(self):
        fetch, calls = self.refusingPublisher(set())
        self.patchSearches(A=lambda s: [self.paper("One", "https://doi.org/10.1/one"), self.paper("Two", "https://doi.org/10.1/two")])
        loop = self.makeLoop([], [], ["A"])
        loop.papers = loop.searchPapers()
        with mock.patch.object(harness_utils, "fetchUrl", fetch):
            loop.readPublisherPages()
        self.assertEqual(loop.logins_asked, ["publisher.test"])
        self.assertEqual([paper["summary"] for paper in loop.papers], ["short", "short"])
        self.assertTrue(all(login is None for url, login in calls))

    def testWrongAccountIsDroppedAfterOneFailureToAvoidLockingIt(self):
        fetch, calls = self.refusingPublisher(set())
        self.patchSearches(A=lambda s: [self.paper(name, f"https://doi.org/10.1/{name}") for name in ("one", "two", "three")])
        loop = self.makeLoop([], [], ["A"])
        loop.nextLogin = ("me", "wrong")
        loop.papers = loop.searchPapers()
        with mock.patch.object(harness_utils, "fetchUrl", fetch):
            loop.readPublisherPages()
        self.assertEqual(sum(login == ("me", "wrong") for url, login in calls), 1)
        self.assertEqual(loop.logins_asked, ["publisher.test"])

    def testAccountsNeverReachTheSavedFile(self):
        fetch, calls = self.refusingPublisher({("me", "secret")})
        self.patchSearches(A=lambda s: [self.paper("One", "https://doi.org/10.1/one")])
        loop = self.makeLoop(["https://doi.org/10.1/one"], ["yes"], ["A"])
        loop.nextLogin = ("me", "secret")
        with mock.patch.object(harness_utils, "fetchUrl", fetch):
            loop.run()
        self.assertNotIn("secret", (self.folder / "literature_contexts.json").read_text(encoding="utf-8"))


class DocumentFormatTests(LoopTestCase):
    def makeLoop(self, replies, answers):
        path = self.folder / "notes.md"
        path.write_text("Introduction\nThis is the first paragraph with some words.\nConclusion\nThat is all.", encoding="utf-8")
        return self.script(DocumentFormatLoop(FakeAgent(replies), path, "markdown with headings"), answers), path

    def testLayoutOnlyChangeIsSavedNextToTheUntouchedOriginal(self):
        loop, path = self.makeLoop(["# Introduction\n\nThis is the first paragraph with some words.\n\n# Conclusion\n\nThat is all."], ["yes"])
        loop.run()
        self.assertTrue((self.folder / "notes_formatted.md").read_text(encoding="utf-8").startswith("# Introduction"))
        self.assertTrue(path.read_text(encoding="utf-8").startswith("Introduction\nThis is"))

    def testChangedWordsAreSentBack(self):
        loop, path = self.makeLoop(["# Intro\n\nThis is a rewritten paragraph.\n\n# End\n\nDone.", "# Introduction\n\nThis is the first paragraph with some words.\n\n# Conclusion\n\nThat is all."], ["yes"])
        self.assertTrue(loop.run().startswith("# Introduction"))
        self.assertIn("changed or removed words", loop.agent.prompts[1])

    def testRejectedDocumentWritesNothing(self):
        loop, path = self.makeLoop(["# Introduction\n\nThis is the first paragraph with some words.\n\n# Conclusion\n\nThat is all."], ["no"])
        self.assertIsNone(loop.run())
        self.assertFalse((self.folder / "notes_formatted.md").exists())


class MathCheckTests(LoopTestCase):
    document = ("\\begin{lemma}Every even number greater than 2 is composite.\\end{lemma}\n"
                "\\begin{proof}Let n be even and larger than 2. Then 2 divides n and n/2 is greater than 1,\nso n is composite.\\end{proof}")

    def finding(self, status="VALID", quote="Then 2 divides n and n/2 is greater than 1, so n is composite.", why="Follows from the definition."):
        return f"CLAIM: Lemma 1\nQUOTE: {quote}\nSTATUS: {status}\nWHY: {why}\n"

    def makeLoop(self, replies, answers):
        path = self.folder / "paper.tex"
        path.write_text(self.document, encoding="utf-8")
        return self.script(MathCheckLoop(FakeAgent(replies), path), answers), path

    def testValidReportIsSavedWithoutCallingASecondReferee(self):
        report = self.finding() + "\nVERDICT: NO PROBLEMS FOUND"
        loop, path = self.makeLoop([report], ["yes"])
        self.assertEqual(loop.run(), report)
        self.assertEqual(len(loop.agent.prompts), 1)
        self.assertEqual((self.folder / "paper_math_report.md").read_text(encoding="utf-8"), report)

    def testQuoteWrappedOverSeveralLinesStillMatches(self):
        loop, path = self.makeLoop([self.finding(quote="Then 2 divides n and n/2 is greater than 1,  so n is composite.") + "\nVERDICT: NO PROBLEMS FOUND"], ["yes"])
        self.assertIsNotNone(loop.run())

    def testInventedQuotesAreCaught(self):
        good = self.finding() + "\nVERDICT: NO PROBLEMS FOUND"
        loop, path = self.makeLoop([self.finding(quote="By Zorn's lemma the claim follows.") + "\nVERDICT: NO PROBLEMS FOUND", good], ["yes"])
        self.assertEqual(loop.run(), good)
        self.assertIn("is not in the document", loop.agent.prompts[1])

    def testBadFormatMissingVerdictAndUnknownStatusAreSentBack(self):
        good = self.finding() + "\nVERDICT: NO PROBLEMS FOUND"
        replies = ["**CLAIM:** Lemma 1 looks fine", self.finding(), self.finding(status="MAYBE") + "\nVERDICT: PROBLEMS FOUND", good]
        loop, path = self.makeLoop(replies, ["yes"])
        self.assertEqual(loop.run(), good)
        self.assertIn("exactly like this", loop.agent.prompts[1])
        self.assertIn("exactly like this", loop.agent.prompts[2])
        self.assertIn("needs a STATUS", loop.agent.prompts[3])

    def testVerdictMustMatchTheFindings(self):
        good = self.finding() + "\nVERDICT: NO PROBLEMS FOUND"
        loop, path = self.makeLoop([self.finding() + "\nVERDICT: PROBLEMS FOUND", good], ["yes"])
        self.assertEqual(loop.run(), good)
        self.assertIn("does not match your findings", loop.agent.prompts[1])

    def testUnreviewedStatementsAreSentBack(self):
        document = self.document + "\n\\begin{theorem}Another claim.\\end{theorem}\n\\begin{corollary}And another.\\end{corollary}"
        path = self.folder / "big.tex"
        path.write_text(document, encoding="utf-8")
        good = self.finding() + "\n" + self.finding().replace("Lemma 1", "Theorem 2") + "\n" + self.finding().replace("Lemma 1", "Corollary 3") + "\nVERDICT: NO PROBLEMS FOUND"
        loop = self.script(MathCheckLoop(FakeAgent([self.finding() + "\nVERDICT: NO PROBLEMS FOUND", good]), path), ["yes"])
        self.assertEqual(loop.run(), good)
        self.assertIn("states 3 theorems", loop.agent.prompts[1])

    def testFlaggedErrorsMustSurviveASecondReferee(self):
        flagged = self.finding(status="ERROR", why="n/2 could equal 1") + "\nVERDICT: PROBLEMS FOUND"
        fixed = self.finding() + "\nVERDICT: NO PROBLEMS FOUND"
        loop, path = self.makeLoop([flagged, "REJECTED: n is larger than 2, so n/2 is greater than 1.", fixed], ["yes"])
        self.assertEqual(loop.run(), fixed)
        self.assertIn("skeptical referee", loop.agent.prompts[1])
        self.assertIn("A second referee rejected your finding", loop.agent.prompts[2])

    def testConfirmedErrorsAreKept(self):
        flagged = self.finding(status="GAP", why="The case n = 4 is not covered.") + "\nVERDICT: PROBLEMS FOUND"
        loop, path = self.makeLoop([flagged, "CONFIRMED"], ["yes"])
        self.assertEqual(loop.run(), flagged)

    def testUnclearFindingsAreNotChallenged(self):
        report = self.finding(status="UNCLEAR", why="Could not verify.") + "\nVERDICT: PROBLEMS FOUND"
        loop, path = self.makeLoop([report], ["yes"])
        self.assertEqual(loop.run(), report)
        self.assertEqual(len(loop.agent.prompts), 1)

    def testMathCheckerRulesAreInThePrompt(self):
        loop, path = self.makeLoop([self.finding() + "\nVERDICT: NO PROBLEMS FOUND"], ["yes"])
        loop.run()
        self.assertIn("Do not check routine arithmetic", loop.agent.prompts[0])
        self.assertNotIn("CALC", loop.agent.prompts[0])


class CoderTests(LoopTestCase):
    def makeLoop(self, replies, answers, name="solution.py", **options):
        path = self.folder / name
        return self.script(CoderLoop(FakeAgent(replies), "print ok", path, **options), answers), path

    def testFailingCodeIsFixedUsingTheErrorMessage(self):
        loop, path = self.makeLoop(["print(1/0)", "print('ok')"], ["yes", "yes"])
        self.assertEqual(loop.run(), "print('ok')")
        self.assertIn("ZeroDivisionError", loop.agent.prompts[1])
        self.assertEqual(path.read_text(encoding="utf-8"), "print('ok')")

    def testFencedCodeIsCleaned(self):
        loop, path = self.makeLoop(["Here you go:\n```python\nprint('x')\n```\nEnjoy"], ["yes", "yes"])
        loop.run()
        self.assertEqual(path.read_text(encoding="utf-8"), "print('x')")

    def testNothingHappensWithoutConsent(self):
        loop, path = self.makeLoop(["print('ok')"], ["no"])
        self.assertIsNone(loop.run())
        self.assertFalse(path.exists())
        self.assertEqual(loop.agent.prompts, [])

    def testRejectedCodeRestoresTheOriginalFile(self):
        loop, path = self.makeLoop(["print('new')"], ["yes", "no"])
        path.write_text("print('original')", encoding="utf-8")
        self.assertIsNone(loop.run())
        self.assertEqual(path.read_text(encoding="utf-8"), "print('original')")
        self.assertIn("print('original')", loop.agent.prompts[0])

    def testRejectedNewFileIsRemoved(self):
        loop, path = self.makeLoop(["print('new')"], ["yes", "no"])
        self.assertIsNone(loop.run())
        self.assertFalse(path.exists())

    def testEndlessLoopsAreStopped(self):
        loop, path = self.makeLoop(["while True: pass", "print('done')"], ["yes", "yes"])
        with mock.patch.object(harness_utils, "RUN_TIMEOUT", 1):
            self.assertEqual(loop.run(), "print('done')")
        self.assertIn("ran for more than 1 seconds", loop.agent.prompts[1])

    def testCustomTestCommandDecidesWhatPasses(self):
        path = self.folder / "answer.txt"
        command = [sys.executable, "-c", f"assert open(r'{path}').read().strip() == '42', 'wrong answer'"]
        loop = self.script(CoderLoop(FakeAgent(["41", "42"]), "write the answer", path, testCommand=command), ["yes", "yes"])
        self.assertEqual(loop.run(), "42")
        self.assertIn("wrong answer", loop.agent.prompts[1])

    def testConsentShowsTheExactCommand(self):
        questions = []
        loop, path = self.makeLoop([], [])
        loop.askUser = lambda question: questions.append(question) or "no"
        loop.run()
        self.assertIn(str(path), questions[0])
        self.assertIn(sys.executable, questions[0])


class ModelCostTests(unittest.TestCase):
    prices = {"claude-opus-5-5": {"input_cost_per_token": 4e-06, "output_cost_per_token": 2e-05, "cache_read_input_token_cost": 4e-07, "max_input_tokens": 1000000},
              "gpt-4o-mini": {"input_cost_per_token": 1.5e-07, "output_cost_per_token": 6e-07},
              "gemini/gemini-3.8-flash": {"input_cost_per_token": 7.5e-07, "output_cost_per_token": 3.75e-06},
              "deepseek-v4-pro": {"input_cost_per_token": 1.32e-06, "output_cost_per_token": 3.96e-06},
              "sample_spec": {"notes": "documentation of the file"}, "embedding-only": {"input_cost_per_token": 1e-07}, "broken": "not a dictionary"}

    def setUp(self):
        self.downloads = []
        self.failure = None
        def fetch(url, login=None, limit=0):
            self.downloads.append((url, limit))
            if self.failure:
                raise self.failure
            return json.dumps(self.prices).encode()
        for patcher in (mock.patch.object(harness_utils, "fetchUrl", fetch), mock.patch.dict(harness_utils.modelPriceCache, {"loaded": 0, "prices": {}})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def testPricesAreInDollarsPerMillionTokens(self):
        cost = harness_utils.getModelCost("claude", "claude-opus-5-5")
        self.assertEqual((cost["input"], cost["output"], cost["cachedInput"], cost["context"]), (4.0, 20.0, 0.4, 1000000))
        self.assertEqual(cost["unit"], "US dollars per 1 million tokens")
        self.assertEqual(cost["page"], "https://platform.claude.com/docs/en/about-claude/pricing")
        self.assertNotIn("error", cost)

    def testNumbersAreCleanAndMissingDetailsAreNone(self):
        cost = harness_utils.getModelCost("gpt", "gpt-4o-mini")
        self.assertEqual((cost["input"], cost["output"], cost["cachedInput"], cost["context"]), (0.15, 0.6, None, None))

    def testNamesWithTheProviderInFrontAreFound(self):
        self.assertEqual(harness_utils.getModelCost("gemini", "gemini-3.8-flash")["output"], 3.75)

    def testOnlyDeepSeekCarriesAPeakHoursNote(self):
        self.assertIn("peak hours", harness_utils.getModelCost("deepseek", "deepseek-v4-pro")["note"])
        self.assertEqual(harness_utils.getModelCost("gpt", "gpt-4o-mini")["note"], "")

    def testUnknownModelsAndEntriesWithoutPricesExplainThemselves(self):
        for model in ("a-model-nobody-knows", "sample_spec", "embedding-only", "broken"):
            cost = harness_utils.getModelCost("gpt", model)
            self.assertIn("No price is published", cost["error"], model)
            self.assertEqual(cost["page"], "https://developers.openai.com/api/docs/pricing")
        self.assertEqual(harness_utils.getModelCost("a-new-provider", "x")["page"], "")

    def testThePriceListIsDownloadedOnceAnHourAndAllowedToBeBig(self):
        harness_utils.getModelCost("gpt", "gpt-4o-mini")
        harness_utils.getModelCost("claude", "claude-opus-5-5")
        self.assertEqual(len(self.downloads), 1)
        self.assertEqual(self.downloads[0][1], harness_utils.PRICE_FILE_LIMIT)
        self.assertGreater(harness_utils.PRICE_FILE_LIMIT, harness_utils.MAX_DOWNLOAD)
        harness_utils.modelPriceCache["loaded"] -= 4000
        harness_utils.getModelCost("gpt", "gpt-4o-mini")
        self.assertEqual(len(self.downloads), 2)

    def testPriceChangesAreSeenAfterTheHour(self):
        self.assertEqual(harness_utils.getModelCost("gpt", "gpt-4o-mini")["input"], 0.15)
        self.prices = {**self.prices, "gpt-4o-mini": {"input_cost_per_token": 3e-07, "output_cost_per_token": 6e-07}}
        harness_utils.modelPriceCache["loaded"] -= 4000
        self.assertEqual(harness_utils.getModelCost("gpt", "gpt-4o-mini")["input"], 0.3)

    def testNoInternetIsReportedInPlainWordsNotRaised(self):
        self.failure = urllib.error.URLError("no network")
        cost = harness_utils.getModelCost("claude", "claude-opus-5-5")
        self.assertIn("could not be fetched: the website could not be reached", cost["error"])
        self.assertEqual(cost["page"], "https://platform.claude.com/docs/en/about-claude/pricing")

    def testBrokenPriceFileIsReportedNotRaised(self):
        self.prices = "this is not what was expected"
        self.assertIn("could not be fetched", harness_utils.getModelCost("gpt", "gpt-4o-mini")["error"])
        self.failure = ValueError("bad data")
        self.assertIn("bad data", harness_utils.getModelCost("gpt", "gpt-4o-mini")["error"])

    def testTheLastPricesAreKeptWhenARefreshFails(self):
        harness_utils.getModelCost("gpt", "gpt-4o-mini")
        harness_utils.modelPriceCache["loaded"] -= 4000
        self.failure = urllib.error.HTTPError("u", 503, "Unavailable", {}, None)
        cost = harness_utils.getModelCost("gpt", "gpt-4o-mini")
        self.assertEqual(cost["input"], 0.15)
        self.assertNotIn("error", cost)


class SwarmTests(LoopTestCase):
    def worker(self, result="done", fail=False, barrier=None, record=None, name=""):
        loop = Loop(FakeAgent())
        def run():
            if record is not None:
                record.append(("start", name))
            if barrier is not None:
                barrier.wait(timeout=5)
            if fail:
                raise RuntimeError("boom")
            if record is not None:
                record.append(("end", name))
            return result
        loop.run = run
        return loop

    def makeSwarm(self, **workers):
        swarm = Swarm("Test mission")
        swarm.addAgent("Leader", self.worker("final"), "boss", "finish")
        for name, loop in workers.items():
            swarm.addAgent(name, loop, f"role {name}", f"task {name}")
        return swarm

    def testIndependentAgentsReallyWorkAtTheSameTime(self):
        barrier = threading.Barrier(3)
        swarm = self.makeSwarm(A=self.worker(barrier=barrier), B=self.worker(barrier=barrier), C=self.worker(barrier=barrier))
        self.assertEqual(swarm.run(), "final")
        self.assertEqual(set(swarm.getStatuses().values()), {"done"})

    def testActiveAndInactiveAgentsCanBeFollowedWhileRunning(self):
        gate, seen = threading.Event(), {}
        def blocked():
            seen["active"] = swarm.getActiveAgents()
            seen["inactive"] = swarm.getInactiveAgents()
            gate.wait(timeout=5)
            return "x"
        blocking = Loop(FakeAgent())
        blocking.run = blocked
        swarm = self.makeSwarm(Busy=blocking, Idle=self.worker())
        threading.Timer(0.3, gate.set).start()
        swarm.run()
        self.assertIn("Busy", seen["active"])
        self.assertIn("Leader", seen["inactive"])

    def testOnlyAgentsThatNeedAnOutputWait(self):
        record = []
        swarm = Swarm("Pipeline")
        swarm.addAgent("Leader", self.worker("final"), "leader", "finish")
        swarm.addAgent("Writer", self.worker("the text", record=record, name="Writer"), "writer", "write")
        swarm.addAgent("Checker", self.worker("checked", record=record, name="Checker"), "checker", "check")
        swarm.addAgent("Formatter", self.worker("formatted", record=record, name="Formatter"), "formatter", "format", waitsFor=["Writer"])
        swarm.addAgent("Mailer", self.worker("sent", record=record, name="Mailer"), "mailer", "mail", waitsFor=["Formatter", "Checker"])
        self.assertEqual(swarm.getStages(), [["Writer", "Checker"], ["Formatter"], ["Mailer"]])
        swarm.run()
        self.assertLess(record.index(("end", "Writer")), record.index(("start", "Formatter")))
        self.assertLess(record.index(("end", "Formatter")), record.index(("start", "Mailer")))
        self.assertLess(record.index(("end", "Checker")), record.index(("start", "Mailer")))
        self.assertTrue(any("Writer: the text" in line for line in swarm.members["Formatter"]["agent"].inbox))
        self.assertTrue(any("Waits for: Writer." in line for line in swarm.members["Formatter"]["agent"].inbox))

    def testMessagesAndConnectionsAreLogged(self):
        swarm = self.makeSwarm(Writer=self.worker("text"), Formatter=self.worker("formatted"))
        swarm.setWaitsFor("Formatter", ["Writer"])
        swarm.run()
        self.assertEqual([message["message"] for message in swarm.getMessages("Writer", "Formatter")], ["text"])
        self.assertEqual(len(swarm.getMessages(receiver="Leader")), 2)
        self.assertEqual(len(swarm.getMessages(sender="Leader")), 2)
        connections = {(link["from"], link["to"]): link["count"] for link in swarm.getConnections()}
        self.assertEqual(connections[("Writer", "Formatter")], 1)
        self.assertEqual(connections[("Formatter", "Leader")], 1)
        self.assertEqual(set(swarm.getMessages()[0]), {"time", "sender", "receiver", "message"})

    def testFailuresDoNotStopTheOthersAndTheLeaderIsTold(self):
        swarm = self.makeSwarm(Bad=self.worker(fail=True), Free=self.worker("fine"))
        swarm.addAgent("Dependent", self.worker("never"), "dep", "needs bad", waitsFor=["Bad"])
        swarm.run()
        self.assertEqual(swarm.getStatuses(), {"Leader": "done", "Bad": "failed", "Free": "done", "Dependent": "failed"})
        self.assertIn("RuntimeError: boom", swarm.getInfo("Bad")["error"])
        self.assertIn("Bad did not finish", swarm.getInfo("Dependent")["error"])
        self.assertTrue(any("FAILED" in message["message"] for message in swarm.getMessages(sender="Dependent", receiver="Leader")))

    def testAnAgentThatEndsWithoutAResultIsFailed(self):
        swarm = self.makeSwarm(Declined=self.worker(result=None))
        swarm.run()
        self.assertEqual(swarm.getStatus("Declined"), "failed")
        self.assertIn("did not approve", swarm.getInfo("Declined")["error"])

    def testImpossiblePlansAreRefusedInsteadOfHanging(self):
        swarm = self.makeSwarm(A=self.worker(), B=self.worker())
        swarm.setWaitsFor("A", ["B"])
        swarm.setWaitsFor("B", ["A"])
        with self.assertRaisesRegex(ValueError, "circle"):
            swarm.run()
        swarm.setWaitsFor("B", [])
        swarm.setWaitsFor("A", ["Nobody"])
        with self.assertRaisesRegex(ValueError, "cannot wait for Nobody"):
            swarm.getStages()
        swarm.setWaitsFor("A", ["Leader"])
        with self.assertRaisesRegex(ValueError, "leader"):
            swarm.getStages()
        with self.assertRaises(ValueError):
            swarm.addAgent("A", self.worker(), "again", "again")
        with self.assertRaises(ValueError):
            Swarm("empty").run()
        self.assertEqual(Swarm("empty").getTree(), {})

    def testTheTreeFollowsTheBosses(self):
        swarm = self.makeSwarm(Author=self.worker(), Mailer=self.worker())
        swarm.addAgent("Reviewer", self.worker(), "reviewer", "review", boss="Author")
        tree = swarm.getTree()
        self.assertEqual(tree["name"], "Leader")
        self.assertEqual([child["name"] for child in tree["children"]], ["Author", "Mailer"])
        self.assertEqual(tree["children"][0]["children"][0]["name"], "Reviewer")
        self.assertEqual(json.loads(json.dumps(tree))["children"][0]["status"], "waiting")
        self.assertEqual((swarm.getParent("Reviewer"), swarm.getParent("Leader"), swarm.getChildren("Leader")), ("Author", None, ["Author", "Mailer"]))
        self.assertEqual((swarm.getLeader(), swarm.getRole("Mailer"), swarm.getTask("Mailer")), ("Leader", "role Mailer", "task Mailer"))
        self.assertEqual(swarm.getAgents(), ["Leader", "Author", "Mailer", "Reviewer"])
        swarm.setLeader("Mailer")
        self.assertEqual(sorted(child["name"] for child in swarm.getTree()["children"]), ["Author", "Leader"])
        self.assertTrue(swarm.getInfo("Mailer")["isLeader"])
        json.dumps(swarm.getInfo("Reviewer"))

    # An agent that stays in its first answer until released, so the test can type a message at that moment.
    def blockingAgent(self, replies, started, release):
        agent = FakeAgent(replies)
        answer = agent.input
        def slowInput(prompt):
            if not agent.prompts:
                started.set()
                release.wait(timeout=5)
            return answer(prompt)
        agent.input = slowInput
        return agent

    def testAMessageToAWorkingAgentIsReadWithoutStoppingIt(self):
        started, release = threading.Event(), threading.Event()
        writer = self.script(AuthorLoop(self.blockingAgent(["first", "second"], started, release), "topic", 50), [])
        leader = self.script(self.worker("final"), ["yes"])
        leader.agent.replies.append("Summary of Writer")
        swarm = Swarm("Test mission")
        swarm.addAgent("Leader", leader, "boss", "finish")
        swarm.addAgent("Writer", writer, "role Writer", "task Writer")
        thread = threading.Thread(target=swarm.run, daemon=True)
        thread.start()
        self.assertTrue(started.wait(timeout=5))
        self.assertEqual(swarm.getStatus("Writer"), "working")
        swarm.sendUserMessage("Writer", "  Mention the budget ")
        release.set()
        thread.join(timeout=5)
        self.assertEqual((swarm.getStatus("Writer"), swarm.getInfo("Writer")["result"]), ("done", "second"))
        self.assertNotIn("budget", writer.agent.prompts[0])
        self.assertIn("- Mention the budget", writer.agent.prompts[1])
        self.assertEqual([message["message"] for message in swarm.getMessages(USER_NAME, "Writer")], ["Mention the budget"])
        self.assertFalse([link for link in swarm.getConnections() if USER_NAME in (link["from"], link["to"])])

    def testTheLeaderIsToldWhileItsAgentsWork(self):
        started, release = threading.Event(), threading.Event()
        def blocked():
            started.set()
            release.wait(timeout=5)
            return "x"
        busy = Loop(FakeAgent())
        busy.run = blocked
        leader = self.script(AuthorLoop(FakeAgent(["final text"]), "mission", 50), ["yes"])
        swarm = Swarm("Test mission")
        swarm.addAgent("Leader", leader, "boss", "finish")
        swarm.addAgent("Busy", busy, "busy", "work")
        thread = threading.Thread(target=swarm.run, daemon=True)
        thread.start()
        self.assertTrue(started.wait(timeout=5))
        self.assertEqual(swarm.getStatus("Leader"), "waiting")
        swarm.sendUserMessage("Leader", "Keep the final text short")
        release.set()
        thread.join(timeout=5)
        self.assertEqual(swarm.getInfo("Leader")["result"], "final text")
        self.assertIn("- Keep the final text short", leader.agent.prompts[0])
        self.assertIn("Busy:", leader.agent.prompts[0])

    def testMessagesThatCannotBeReadAreRefused(self):
        swarm = self.makeSwarm(Writer=self.worker(), Bad=self.worker(fail=True))
        with self.assertRaisesRegex(ValueError, "no agent called Ghost"):
            swarm.sendUserMessage("Ghost", "hello")
        with self.assertRaisesRegex(ValueError, "empty"):
            swarm.sendUserMessage("Writer", "   ")
        swarm.run()
        for name in ("Writer", "Bad", "Leader"):
            with self.assertRaisesRegex(ValueError, "already finished"):
                swarm.sendUserMessage(name, "too late")
        self.assertEqual(swarm.getMessages(sender=USER_NAME), [])
        with self.assertRaisesRegex(ValueError, "name of the user"):
            swarm.addAgent(USER_NAME, self.worker(), "role", "task")

    def testARunStartsWithoutTheMessagesOfTheLastOne(self):
        writer = self.worker()
        swarm = self.makeSwarm(Writer=writer)
        swarm.sendUserMessage("Writer", "old news")
        self.assertEqual(writer.userMessages, ["old news"])
        swarm.run()
        self.assertEqual(writer.userMessages, [])
        self.assertEqual(swarm.getMessages(sender=USER_NAME), [])

    def planSwarm(self, replies, answers):
        leader = self.script(Loop(FakeAgent(replies)), answers)
        swarm = Swarm("Plan")
        swarm.addAgent("Leader", leader, "leader", "lead")
        for name in ("Writer", "Formatter", "Checker"):
            swarm.addAgent(name, self.worker(), name.lower(), name.lower())
        return swarm

    def testTheLeaderPlansWhoWaitsAndTheUserApproves(self):
        swarm = self.planSwarm(['```json\n{"Writer": [], "Formatter": ["Writer"], "Checker": []}\n```'], ["yes"])
        self.assertTrue(swarm.planWithLeader())
        self.assertEqual(swarm.getStages(), [["Writer", "Checker"], ["Formatter"]])

    def testBadPlansAreSentBackToTheLeader(self):
        replies = ["not json", '{"Writer": [], "Formatter": ["Ghost"], "Checker": []}', '{"Writer": ["Formatter"], "Formatter": ["Writer"], "Checker": []}',
                   '{"Writer": [], "Formatter": [], "Checker": []}']
        swarm = self.planSwarm(replies, ["yes"])
        self.assertTrue(swarm.planWithLeader())
        self.assertEqual(swarm.getStages(), [["Writer", "Formatter", "Checker"]])

    def testRejectedPlanChangesNothing(self):
        swarm = self.planSwarm(['{"Writer": [], "Formatter": ["Writer"], "Checker": []}'], ["no"])
        self.assertFalse(swarm.planWithLeader())
        self.assertEqual(swarm.getInfo("Formatter")["waitsFor"], [])

    def testRealLoopsInASwarmAreApprovedThroughTheSummaryOfTheLeader(self):
        log = []
        swarm = Swarm("Parallel dialogues")
        swarm.addAgent("Boss", AuthorLoop(FakeAgent(["summary one: One Two Three Four", "text boss", "summary two: One Two Three Four Boss"]), "boss", 50), "boss", "lead")
        for name in ("One", "Two", "Three", "Four"):
            swarm.addAgent(name, AuthorLoop(FakeAgent([f"text {name}"]), name.lower(), 50), name, name)
        def fakeInput(question):
            threading.Event().wait(0.03)
            log.append(("ask", question))
            return "yes"
        with mock.patch.object(harness_utils, "print", lambda message: log.append(("show", message)), create=True), \
                mock.patch.object(harness_utils, "input", fakeInput, create=True):
            swarm.run()
        starts = [index for index, entry in enumerate(log) if entry[0] == "show" and "Summary of the results" in entry[1]]
        self.assertEqual(len(starts), 2)
        for index in starts:
            self.assertIn("clicking on its name", log[index + 1][1])
            self.assertEqual(log[index + 2][0], "ask")
        self.assertEqual(len([entry for entry in log if entry[0] == "ask"]), 2)
        self.assertEqual(set(loadContext("author_contexts.json")), {"boss", "one", "two", "three", "four"})

    # A swarm of loops that only draft, so a test can follow the plans and the drafts. The leader writes the summaries and the user answers them.
    def reviewedSwarm(self, mode, leaderReplies, answers, summaries=True, **workers):
        swarm = Swarm("Test mission")
        leader = self.script(DraftLoop(FakeAgent(leaderReplies)), answers)
        if summaries:
            self.addCleanup(lambda: self.assertNotIn("could not write the summary", self.saidText(leader)))
        swarm.addAgent("Leader", leader, "boss", "finish")
        for name, replies in workers.items():
            swarm.addAgent(name, self.script(DraftLoop(FakeAgent(replies)), []), f"role {name}", f"task {name}")
        swarm.setMode(mode)
        return swarm

    def agentOf(self, swarm, name):
        return swarm.members[name]["agent"]

    # Waits, from inside the question to the user, until an agent has written a new draft after the revision it had.
    def waitForNewDraft(self, swarm, name, revision):
        with swarm.changed:
            self.assertTrue(swarm.changed.wait_for(lambda: swarm.members[name]["revision"] > revision and swarm.members[name]["review"] == "ready", timeout=5))

    def testTheModeIsPlanOrExecute(self):
        swarm = Swarm("Modes")
        self.assertEqual(swarm.getMode(), "execute")
        swarm.setMode("plan")
        self.assertEqual(swarm.getMode(), "plan")
        with self.assertRaises(ValueError):
            swarm.setMode("sleep")

    def testPlanModeSummarisesAllThePlansForTheUser(self):
        swarm = self.reviewedSwarm("plan", ["plan of the leader", "Summary: Leader, Writer and Mailer"], ["yes"], Writer=["plan of the writer"], Mailer=["plan of the mailer"])
        leader, seen = self.agentOf(swarm, "Leader"), {}
        ask = leader.askUser
        def askAndLook(question):
            seen["ready"] = swarm.getReadyAgents()
            seen["lights"] = [swarm.getTree()["review"]] + [child["review"] for child in swarm.getTree()["children"]]
            seen["statuses"] = swarm.getStatuses()
            return ask(question)
        leader.askUser = askAndLook
        self.assertEqual(swarm.run(), "Summary: Leader, Writer and Mailer")
        self.assertEqual(sorted(seen["ready"]), ["Leader", "Mailer", "Writer"])
        self.assertEqual(seen["lights"], ["ready"] * 3)
        self.assertEqual(set(seen["statuses"].values()), {"working"})
        self.assertEqual(set(swarm.getStatuses().values()), {"done"})
        self.assertEqual(swarm.getSummary(), "Summary: Leader, Writer and Mailer")
        info = swarm.getInfo("Writer")
        self.assertEqual((info["plan"], info["review"], info["mode"], info["draft"]), ("plan of the writer", "approved", "plan", "plan of the writer"))
        self.assertIn("Write a plan for your task: task Writer", self.agentOf(swarm, "Writer").agent.prompts[0])
        summaryPrompt = leader.agent.prompts[1]
        self.assertTrue(all(plan in summaryPrompt for plan in ("plan of the leader", "plan of the writer", "plan of the mailer")))
        self.assertIn("Summary of the plans", self.saidText(leader))
        self.assertIn("by clicking on its name in the swarm", self.saidText(leader))
        self.assertIsNone(leader.reviewer)

    def testACorrectionOfTheSummaryIsSentByTheLeaderToTheConcernedAgents(self):
        swarm = self.reviewedSwarm("plan", ["plan of the leader", "Summary one: Leader Writer Mailer", '{"Writer": "Make it shorter"}', "Summary two: Leader Writer Mailer"],
                                   ["Make the plan of the writer shorter", "yes"], Writer=["a long plan", "a short plan"], Mailer=["plan of the mailer"])
        self.assertEqual(swarm.run(), "Summary two: Leader Writer Mailer")
        writer, mailer, leader = (self.agentOf(swarm, name) for name in ("Writer", "Mailer", "Leader"))
        self.assertEqual(swarm.getInfo("Writer")["plan"], "a short plan")
        self.assertEqual(len(writer.agent.prompts), 2)
        self.assertEqual(len(mailer.agent.prompts), 1)
        correction = "The user asked for a correction: Make the plan of the writer shorter\nWhat to change in your plan: Make it shorter"
        self.assertIn(f"Leader: {correction}", writer.agent.prompts[1])
        self.assertIn("Your previous draft:\na long plan", writer.agent.prompts[1])
        self.assertIn("It is in your newest messages", writer.agent.prompts[1])
        self.assertFalse(any("correction" in line for line in mailer.inbox))
        self.assertIn("a short plan", leader.agent.prompts[3])
        self.assertNotIn("a long plan", leader.agent.prompts[3])
        self.assertIn("Asking Writer to correct their plan.", leader.said)
        self.assertEqual([message["message"] for message in swarm.getMessages("Leader", "Writer")][-1], correction)

    def testACorrectionAboutOnlyTheSummaryChangesNoPlan(self):
        swarm = self.reviewedSwarm("plan", ["leader plan", "Summary one: Leader Writer", "{}", "Summary two: Leader Writer"],
                                   ["Use shorter sentences in the summary", "yes"], Writer=["writer plan"])
        self.assertEqual(swarm.run(), "Summary two: Leader Writer")
        self.assertEqual(len(self.agentOf(swarm, "Writer").agent.prompts), 1)
        self.assertIn("the summary, if anything: Use shorter sentences in the summary", self.agentOf(swarm, "Leader").agent.prompts[3])
        self.assertIn("No agent is concerned", self.saidText(self.agentOf(swarm, "Leader")))

    def testACorrectionGoesToEveryoneIfTheLeaderCannotRouteIt(self):
        swarm = self.reviewedSwarm("plan", ["leader plan", "Summary one: Leader Writer"] + ["not json"] * 5 + ["leader plan two", "Summary two: Leader Writer"],
                                   ["Use bullet points", "yes"], Writer=["writer plan", "writer plan two"])
        self.assertEqual(swarm.run(), "Summary two: Leader Writer")
        self.assertEqual((swarm.getInfo("Writer")["plan"], swarm.getInfo("Leader")["plan"]), ("writer plan two", "leader plan two"))
        self.assertIn("What to change in your plan: Use bullet points", self.agentOf(swarm, "Writer").agent.prompts[1])

    def testASummaryThatForgetsAnAgentIsWrittenAgain(self):
        swarm = self.reviewedSwarm("plan", ["leader plan", "Only the Writer", "Leader and Writer"], ["yes"], Writer=["writer plan"])
        swarm.run()
        self.assertIn("you forgot: ['Leader']", self.agentOf(swarm, "Leader").agent.prompts[2])
        self.assertEqual(swarm.getSummary(), "Leader and Writer")

    def testTheDraftsAreShownIfTheLeaderCannotWriteTheSummary(self):
        swarm = self.reviewedSwarm("plan", ["leader plan"], ["yes"], summaries=False, Writer=["writer plan"])
        self.assertIn("- Writer (role Writer): waiting for the user: writer plan", swarm.run())
        self.assertIn("could not write the summary", self.saidText(self.agentOf(swarm, "Leader")))

    def testAnIndividualCorrectionChangesOnlyThatPlanAndTheSummaryIsWrittenAgain(self):
        swarm = self.reviewedSwarm("plan", ["leader plan", "Summary one: Leader Writer Mailer", "Summary two: Leader Writer Mailer"], ["yes"],
                                   Writer=["writer plan", "writer bullets"], Mailer=["mailer plan"])
        leader, calls = self.agentOf(swarm, "Leader"), []
        def askUser(question):
            calls.append(question)
            if len(calls) == 1:
                revision = swarm.members["Writer"]["revision"]
                swarm.correctDraft("Writer", "Use bullet points")
                self.waitForNewDraft(swarm, "Writer", revision)
            return "yes"
        leader.askUser = askUser
        self.assertEqual(swarm.run(), "Summary two: Leader Writer Mailer")
        self.assertEqual(len(calls), 2)
        self.assertIn("changed while you were reading", self.saidText(leader))
        writer = self.agentOf(swarm, "Writer")
        self.assertIn("- Use bullet points", writer.agent.prompts[1])
        self.assertIn("Your previous draft:\nwriter plan", writer.agent.prompts[1])
        self.assertEqual(len(self.agentOf(swarm, "Mailer").agent.prompts), 1)
        self.assertEqual([message["message"] for message in swarm.getMessages(USER_NAME, "Writer")], ["Use bullet points"])
        self.assertIn("writer bullets", leader.agent.prompts[2])
        self.assertEqual(swarm.getInfo("Writer")["plan"], "writer bullets")

    def testAMessageToAnAgentWithAReadyDraftIsACorrection(self):
        swarm = self.reviewedSwarm("plan", ["leader plan", "Summary one: Leader Writer", "Summary two: Leader Writer"], ["yes"], Writer=["writer plan", "writer plan two"])
        leader, calls = self.agentOf(swarm, "Leader"), []
        def askUser(question):
            calls.append(question)
            if len(calls) == 1:
                revision = swarm.members["Writer"]["revision"]
                swarm.sendUserMessage("Writer", "Add the dates")
                self.waitForNewDraft(swarm, "Writer", revision)
            return "yes"
        leader.askUser = askUser
        swarm.run()
        self.assertEqual(swarm.getInfo("Writer")["plan"], "writer plan two")
        self.assertIn("- Add the dates", self.agentOf(swarm, "Writer").agent.prompts[1])

    def testAnAgentCanBeApprovedByItselfWhileTheSummaryWaits(self):
        swarm = self.reviewedSwarm("plan", ["leader plan", "Summary: Leader Writer Mailer"], ["yes"], Writer=["writer plan"], Mailer=["mailer plan"])
        leader, calls = self.agentOf(swarm, "Leader"), []
        def askUser(question):
            calls.append(question)
            swarm.approveDraft("Mailer")
            self.assertEqual(swarm.getInfo("Mailer")["review"], "approved")
            return "yes"
        leader.askUser = askUser
        self.assertEqual(swarm.run(), "Summary: Leader Writer Mailer")
        self.assertEqual((len(calls), set(swarm.getStatuses().values())), (1, {"done"}))
        self.assertNotIn("changed while you were reading", self.saidText(leader))

    def testRejectingTheSummaryRejectsEveryPlan(self):
        swarm = self.reviewedSwarm("plan", ["leader plan", "Summary: Leader Writer"], ["no"], Writer=["writer plan"])
        self.assertIsNone(swarm.run())
        self.assertEqual(set(swarm.getStatuses().values()), {"failed"})
        self.assertIn("did not approve", swarm.getInfo("Writer")["error"])
        self.assertEqual(swarm.getInfo("Writer")["plan"], "")

    def testRejectingOnePlanOnItsOwnLeavesTheAnswerForTheOthers(self):
        swarm = self.reviewedSwarm("plan", ["leader plan", "Summary: Leader Writer Mailer"], ["yes"], Writer=["writer plan"], Mailer=["mailer plan"])
        leader, calls = self.agentOf(swarm, "Leader"), []
        def askUser(question):
            calls.append(question)
            swarm.rejectDraft("Mailer")
            return "yes"
        leader.askUser = askUser
        self.assertIsNone(swarm.run())
        self.assertEqual(len(calls), 1)
        self.assertEqual(swarm.getStatuses(), {"Leader": "done", "Writer": "done", "Mailer": "failed"})
        self.assertEqual(self.agentOf(swarm, "Writer").approvedPlan, "writer plan")
        self.assertEqual(self.agentOf(swarm, "Mailer").approvedPlan, "")

    def testOnlyADraftThatWaitsCanBeDecided(self):
        swarm = self.reviewedSwarm("plan", ["leader plan"], [], Writer=["writer plan"])
        for action in (swarm.approveDraft, swarm.rejectDraft, lambda name: swarm.correctDraft(name, "change")):
            with self.assertRaisesRegex(ValueError, "nothing waiting"):
                action("Writer")
        with self.assertRaisesRegex(ValueError, "no agent called Ghost"):
            swarm.approveDraft("Ghost")

    def testTheApprovedPlansAreFollowedWhenTheSwarmExecutes(self):
        swarm = self.reviewedSwarm("plan", ["leader plan", "Summary: Leader Writer"], ["yes"], Writer=["writer plan"])
        swarm.run()
        leader, writer = self.agentOf(swarm, "Leader"), self.agentOf(swarm, "Writer")
        writer.agent.replies.append("writer draft")
        leader.agent.replies += ["Summary of Writer", "leader draft", "Summary of Writer and Leader"]
        leader.said.clear()
        leader.askUser = lambda question: "yes"
        swarm.setMode("execute")
        self.assertEqual(swarm.run(), "leader draft")
        self.assertIn("Your plan, approved by the user", writer.agent.prompts[-1])
        self.assertIn("writer plan", writer.agent.prompts[-1])
        self.assertEqual((swarm.getInfo("Writer")["plan"], swarm.getInfo("Writer")["mode"]), ("writer plan", "execute"))

    def testExecuteModeAsksTheUserAfterEachGroupOfAgents(self):
        swarm = self.reviewedSwarm("execute", ["Summary one: Writer", "Summary two: Writer Formatter", "leader draft", "Summary three: Writer Formatter Leader"], ["yes", "yes", "yes"],
                                   Writer=["writer draft"], Formatter=["formatter draft"])
        swarm.setWaitsFor("Formatter", ["Writer"])
        leader = self.agentOf(swarm, "Leader")
        self.assertEqual(swarm.run(), "leader draft")
        self.assertEqual(set(swarm.getStatuses().values()), {"done"})
        firstRound, secondRound = leader.agent.prompts[0], leader.agent.prompts[1]
        self.assertIn("- Writer (role Writer): waiting for the user: writer draft", firstRound)
        self.assertIn("- Formatter (role Formatter): has not started yet", firstRound)
        self.assertIn("- Writer (role Writer): done and approved by the user: writer draft", secondRound)
        self.assertIn("- Formatter (role Formatter): waiting for the user: formatter draft", secondRound)
        self.assertIn("Summary of the results", self.saidText(leader))
        self.assertIn("approve or correct the result of each agent", self.saidText(leader))
        self.assertEqual(len(self.agentOf(swarm, "Formatter").agent.prompts), 1)

    def testACorrectionInExecuteModeChangesTheDraftBeforeTheAgentActs(self):
        swarm = self.reviewedSwarm("execute", ["Summary one: Writer", '{"Writer": "Use a formal tone"}', "Summary two: Writer", "leader draft", "Summary three: Writer Leader"],
                                   ["Make it formal", "yes", "yes"], Writer=["casual draft", "formal draft"])
        self.assertEqual(swarm.run(), "leader draft")
        self.assertEqual(swarm.getInfo("Writer")["result"], "formal draft")
        self.assertIn("What to change in your result: Use a formal tone", self.agentOf(swarm, "Writer").agent.prompts[1])

    def testRejectingInExecuteModeFailsTheAgentAndItsDependentsButNotTheLeader(self):
        swarm = self.reviewedSwarm("execute", ["Summary one: Writer", "leader draft", "Summary two: Writer Formatter Leader"], ["no", "yes"],
                                   Writer=["writer draft"], Formatter=["never written"])
        swarm.setWaitsFor("Formatter", ["Writer"])
        self.assertEqual(swarm.run(), "leader draft")
        self.assertEqual(swarm.getStatuses(), {"Leader": "done", "Writer": "failed", "Formatter": "failed"})
        self.assertTrue(any("FAILED" in line for line in self.agentOf(swarm, "Leader").inbox))
        self.assertEqual(len(self.agentOf(swarm, "Formatter").agent.prompts), 0)


    def testAnApprovedAgentGoesOnWithoutWaitingForTheOthers(self):
        swarm = self.reviewedSwarm("execute", ["Summary one: Writer Checker", "Summary two: Writer Checker Formatter", "leader draft", "Summary three: Writer Checker Formatter Leader"],
                                   ["yes", "yes", "yes"], Writer=["writer draft"], Checker=["checker draft"], Formatter=["formatter draft"])
        swarm.setWaitsFor("Formatter", ["Writer"])
        leader, seen, calls = self.agentOf(swarm, "Leader"), {}, []
        ask = leader.askUser
        def askUser(question):
            calls.append(question)
            if len(calls) == 1:
                swarm.approveDraft("Writer")
                with swarm.changed:
                    self.assertTrue(swarm.changed.wait_for(lambda: swarm.members["Formatter"]["review"] == "ready", timeout=5))
                seen["checker"] = swarm.getInfo("Checker")["review"]
            return ask(question)
        leader.askUser = askUser
        self.assertEqual(swarm.run(), "leader draft")
        self.assertEqual(seen["checker"], "ready")
        self.assertEqual((len(calls), set(swarm.getStatuses().values())), (3, {"done"}))
        self.assertTrue(any("Writer: writer draft" in line for line in self.agentOf(swarm, "Formatter").inbox))
        self.assertIn("- Writer (role Writer): done and approved by the user: writer draft", leader.agent.prompts[1])
        self.assertNotIn("changed while you were reading", self.saidText(leader))

    def testWaitsAreIgnoredInPlanMode(self):
        swarm = self.reviewedSwarm("plan", ["leader plan", "Summary: Leader Writer Mailer"], ["yes"], Writer=["writer plan"], Mailer=["mailer plan"])
        swarm.setWaitsFor("Mailer", ["Writer"])
        leader, seen = self.agentOf(swarm, "Leader"), {}
        ask = leader.askUser
        def askUser(question):
            seen["ready"] = sorted(swarm.getReadyAgents())
            return ask(question)
        leader.askUser = askUser
        swarm.run()
        self.assertEqual(seen["ready"], ["Leader", "Mailer", "Writer"])

    def testTheApprovalOfOneAgentIsEchoedInTheNextSummary(self):
        swarm = self.reviewedSwarm("plan", ["leader plan", "Summary one: Leader Writer Mailer", '{"Writer": "Make it shorter"}', "Summary two: Leader Writer Mailer"],
                                   ["Shorten the plan of the writer", "yes"], Writer=["a long plan", "a short plan"], Mailer=["mailer plan"])
        leader, calls = self.agentOf(swarm, "Leader"), []
        ask = leader.askUser
        def askUser(question):
            calls.append(question)
            if len(calls) == 1:
                swarm.approveDraft("Mailer")
            return ask(question)
        leader.askUser = askUser
        swarm.run()
        self.assertIn("- Mailer (role Mailer): done and approved by the user: mailer plan", leader.agent.prompts[3])
        self.assertIn("- Writer (role Writer): waiting for the user: a short plan", leader.agent.prompts[3])
        self.assertEqual(swarm.getInfo("Writer")["plan"], "a short plan")

    def testAnAnswerOnlyAppliesToTheDraftsItWasGivenFor(self):
        swarm = self.reviewedSwarm("plan", ["leader plan", "Summary one: Leader Writer Mailer", "Summary two: Leader Writer Mailer"], ["yes", "yes"],
                                   Writer=["writer plan", "writer bullets"], Mailer=["mailer plan"])
        leader, calls = self.agentOf(swarm, "Leader"), []
        ask = leader.askUser
        def askUser(question):
            calls.append(question)
            if len(calls) == 1:
                revision = swarm.members["Writer"]["revision"]
                swarm.correctDraft("Writer", "Use bullet points")
                self.waitForNewDraft(swarm, "Writer", revision)
            return ask(question)
        leader.askUser = askUser
        swarm.run()
        self.assertEqual(len(calls), 2)
        self.assertIn("The plan of Writer changed while you were reading", self.saidText(leader))
        self.assertIn("- Mailer (role Mailer): done and approved by the user: mailer plan", leader.agent.prompts[2])
        self.assertIn("- Leader (boss): done and approved by the user: leader plan", leader.agent.prompts[2])
        self.assertIn("- Writer (role Writer): waiting for the user: writer bullets", leader.agent.prompts[2])

    def testADecisionCanBeTiedToTheDraftThatWasLookedAt(self):
        swarm = self.reviewedSwarm("plan", ["leader plan", "Summary: Leader Writer"], ["yes"], Writer=["writer plan"])
        leader, ask = self.agentOf(swarm, "Leader"), self.agentOf(swarm, "Leader").askUser
        def askUser(question):
            revision = swarm.getInfo("Writer")["revision"]
            for decide in (swarm.approveDraft, swarm.rejectDraft):
                with self.assertRaisesRegex(ValueError, "new draft"):
                    decide("Writer", revision - 1)
            self.assertEqual(swarm.getInfo("Writer")["review"], "ready")
            swarm.approveDraft("Writer", revision)
            return ask(question)
        leader.askUser = askUser
        swarm.run()
        self.assertEqual(swarm.getInfo("Writer")["review"], "approved")

    # In the console the user types the name of an agent, instead of clicking on it, to decide about it alone.
    def testTheUserCanCorrectOneAgentAloneInTheConsole(self):
        swarm = self.reviewedSwarm("plan", ["leader plan", "Summary one: Leader Writer Mailer", "Summary two: Leader Writer Mailer"], ["writer", "Make it shorter", "yes"],
                                   Writer=["a long plan", "a short plan"], Mailer=["mailer plan"])
        leader = self.agentOf(swarm, "Leader")
        self.assertEqual(swarm.run(), "Summary two: Leader Writer Mailer")
        self.assertIn("[Writer] its plan:\na long plan", leader.said)
        writer = self.agentOf(swarm, "Writer")
        self.assertIn("- Make it shorter", writer.agent.prompts[1])
        self.assertIn("Your previous draft:\na long plan", writer.agent.prompts[1])
        self.assertEqual(len(self.agentOf(swarm, "Mailer").agent.prompts), 1)
        self.assertEqual(leader.agent.replies, [])

    def testTheUserCanApproveOrRejectOneAgentAloneInTheConsole(self):
        swarm = self.reviewedSwarm("plan", ["leader plan", "Summary one: Leader Writer Mailer", "Summary two: Leader Writer Mailer"], ["Mailer", "yes", "yes"],
                                   Writer=["writer plan"], Mailer=["mailer plan"])
        self.assertEqual(swarm.run(), "Summary two: Leader Writer Mailer")
        self.assertIn("- Mailer (role Mailer): done and approved by the user: mailer plan", self.agentOf(swarm, "Leader").agent.prompts[2])
        swarm = self.reviewedSwarm("plan", ["leader plan", "Summary one: Leader Writer Mailer", "Summary two: Leader Writer Mailer"], ["MAILER", "no", "yes"],
                                   Writer=["writer plan"], Mailer=["mailer plan"])
        self.assertIsNone(swarm.run())
        self.assertEqual(swarm.getStatuses(), {"Leader": "done", "Writer": "done", "Mailer": "failed"})
        self.assertIn("- Mailer (role Mailer): rejected by the user", self.agentOf(swarm, "Leader").agent.prompts[2])


    def testAListenerFollowsTheWholeRun(self):
        events = []
        swarm = self.makeSwarm(Writer=self.worker("text"))
        swarm.setWaitsFor("Writer", [])
        swarm.addListener(events.append)
        swarm.run()
        kinds = [event["kind"] for event in events]
        self.assertEqual((kinds[0], kinds[-1]), ("run", "finished"))
        self.assertEqual(events[0]["mode"], "execute")
        self.assertTrue(events[-1]["ok"])
        statuses = [(event["agent"], event["status"]) for event in events if event["kind"] == "status"]
        self.assertEqual(statuses, [("Writer", "working"), ("Writer", "done"), ("Leader", "working"), ("Leader", "done")])
        messages = [(event["sender"], event["receiver"]) for event in events if event["kind"] == "message"]
        self.assertEqual(messages, [("Leader", "Writer"), ("Writer", "Leader")])
        self.assertTrue(all(event["time"] and "agent" in event for event in events))

    def testTheEventsOfAReviewAndOfASummaryAreSent(self):
        events = []
        swarm = self.reviewedSwarm("execute", ["Summary one: Writer", "leader draft", "Summary two: Writer Leader"], ["yes", "yes"], Writer=["writer draft"])
        swarm.addListener(events.append)
        swarm.run()
        reviews = [(event["agent"], event["review"]) for event in events if event["kind"] == "review"]
        self.assertEqual(reviews, [("Writer", "ready"), ("Writer", "approved"), ("Leader", "ready"), ("Leader", "approved")])
        summaries = [event["text"] for event in events if event["kind"] == "summary"]
        self.assertEqual(summaries, ["Summary one: Writer", "Summary two: Writer Leader"])
        rejected = []
        swarm = self.reviewedSwarm("execute", ["Summary: Writer", "leader draft", "Summary: Writer Leader"], ["no", "yes"], Writer=["writer draft"])
        swarm.addListener(rejected.append)
        swarm.run()
        self.assertIn(("Writer", "rejected"), [(event["agent"], event["review"]) for event in rejected if event["kind"] == "review"])
        self.assertIn(("Writer", "failed"), [(event["agent"], event["status"]) for event in rejected if event["kind"] == "status"])

    def testAListenerThatFailsNeverStopsTheSwarm(self):
        seen = []
        def broken(event):
            seen.append(event["kind"])
            raise RuntimeError("the user interface crashed")
        swarm = self.makeSwarm(Writer=self.worker("text"))
        swarm.addListener(broken)
        with mock.patch.object(harness_utils.traceback, "print_exc") as report:
            self.assertEqual(swarm.run(), "final")
        self.assertGreater(len(seen), 4)
        self.assertEqual(report.call_count, len(seen))

    def testTheTreeSaysWhoWaitsForWhomAndWhoIsStillWaitedFor(self):
        started, release, seen = threading.Event(), threading.Event(), {}
        def slow():
            started.set()
            release.wait(timeout=5)
            seen["during"] = {name: swarm.getInfo(name)["waitingOn"] for name in swarm.getAgents()}
            seen["tree"] = swarm.getTree()
            return "text"
        writer = self.worker("text")
        writer.run = slow
        swarm = self.makeSwarm(Writer=writer, Formatter=self.worker("formatted"), Checker=self.worker("checked"))
        swarm.setWaitsFor("Formatter", ["Writer", "Checker"])
        self.assertEqual(swarm.getInfo("Formatter")["waitingOn"], ["Writer", "Checker"])
        self.assertEqual(swarm.getInfo("Leader")["waitingOn"], ["Writer", "Formatter", "Checker"])
        thread = threading.Thread(target=swarm.run, daemon=True)
        thread.start()
        self.assertTrue(started.wait(timeout=5))
        for attempt in range(500):
            if swarm.getStatus("Checker") == "done":
                break
            threading.Event().wait(0.01)
        release.set()
        thread.join(timeout=5)
        self.assertEqual(seen["during"]["Writer"], [])
        self.assertEqual(seen["during"]["Formatter"], ["Writer"])
        self.assertEqual(sorted(seen["during"]["Leader"]), ["Formatter", "Writer"])
        nodes = {child["name"]: child for child in seen["tree"]["children"]}
        self.assertEqual((nodes["Formatter"]["waitsFor"], nodes["Formatter"]["waitingOn"]), (["Writer", "Checker"], ["Writer"]))
        self.assertEqual((seen["tree"]["waitsFor"], nodes["Writer"]["waitsFor"]), ([], []))
        self.assertTrue(all(swarm.getInfo(name)["waitingOn"] == [] for name in swarm.getAgents()))
        json.dumps(swarm.getTree())

    def testNobodyWaitsForAnybodyInPlanMode(self):
        swarm = self.reviewedSwarm("plan", ["leader plan", "Summary: Leader Writer Formatter"], ["yes"], Writer=["writer plan"], Formatter=["formatter plan"])
        swarm.setWaitsFor("Formatter", ["Writer"])
        seen = {}
        leader = self.agentOf(swarm, "Leader")
        ask = leader.askUser
        def askUser(question):
            seen["waiting"] = {name: swarm.getInfo(name)["waitingOn"] for name in swarm.getAgents()}
            seen["ready"] = sorted(swarm.getReadyAgents())
            return ask(question)
        leader.askUser = askUser
        swarm.run()
        self.assertEqual(seen["ready"], ["Formatter", "Leader", "Writer"])
        self.assertTrue(all(waiting == [] for waiting in seen["waiting"].values()))

    def testAnAgentWithATimeOfItsOwnSleepsWithoutHoldingTheOthersBack(self):
        swarm = Swarm("Test mission")
        leader = self.script(DraftLoop(FakeAgent(["Summary one: Early", "Summary two: Early Late", "leader draft", "Summary three: Early Late Leader"])), ["yes", "yes", "yes"])
        early, late = self.script(DraftLoop(FakeAgent(["early draft"])), []), self.script(TimedDraftLoop(FakeAgent(["late draft"]), 30), [])
        for name, loop in (("Leader", leader), ("Early", early), ("Late", late)):
            swarm.addAgent(name, loop, f"role {name}", f"task {name}")
        seen, ask = {}, leader.askUser
        def askUser(question):
            if "first" not in seen:
                seen["first"] = (swarm.getStatus("Late"), swarm.getInfo("Late")["startAt"], swarm.getTree()["children"][1]["startAt"], swarm.getReadyAgents())
                swarm.startNow("Late")
            return ask(question)
        leader.askUser = askUser
        self.assertEqual(swarm.run(), "leader draft")
        status, startAt, treeStartAt, ready = seen["first"]
        self.assertEqual((status, ready), ("waiting", ["Early"]))
        self.assertRegex(startAt, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
        self.assertEqual(treeStartAt, startAt)
        self.assertEqual((swarm.getInfo("Late")["startAt"], swarm.getInfo("Late")["result"]), ("", "late draft"))
        self.assertEqual(set(swarm.getStatuses().values()), {"done"})

    # Runs the swarm in a thread, so a test that would hang fails after a few seconds instead.
    def runInThread(self, swarm):
        outcome = {}
        thread = threading.Thread(target=lambda: outcome.update(result=swarm.run()), daemon=True)
        thread.start()
        return thread, outcome

    def testAnAgentThatSleepsUntilItsTimeIsNotForgottenWhenNobodyElseIsLeft(self):
        swarm = Swarm("Test mission")
        leader = self.script(DraftLoop(FakeAgent(["Summary one: Late", "leader draft", "Summary two: Late Leader"])), ["yes", "yes"])
        late = self.script(TimedDraftLoop(FakeAgent(["late draft"]), 0.4), [])
        swarm.addAgent("Leader", leader, "boss", "finish")
        swarm.addAgent("Late", late, "role", "task")
        thread, outcome = self.runInThread(swarm)
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive(), "The swarm is stuck: nobody waited for the agent that slept.")
        self.assertEqual(outcome["result"], "leader draft")
        self.assertEqual((swarm.getInfo("Late")["result"], swarm.getStatus("Late")), ("late draft", "done"))

    def testTheLeaderWaitsForItsOwnTimeToo(self):
        swarm = Swarm("Test mission")
        leader = self.script(TimedDraftLoop(FakeAgent(["leader draft"]), 0.5), ["yes"])
        swarm.addAgent("Leader", leader, "boss", "finish")
        seen = {}
        thread, outcome = self.runInThread(swarm)
        for attempt in range(300):
            if swarm.getInfo("Leader")["startAt"]:
                seen["startAt"], seen["status"] = swarm.getInfo("Leader")["startAt"], swarm.getStatus("Leader")
                break
            threading.Event().wait(0.01)
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive(), "The swarm is stuck.")
        self.assertEqual(seen["status"], "waiting")
        self.assertRegex(seen["startAt"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
        self.assertEqual((outcome["result"], swarm.getInfo("Leader")["startAt"]), ("leader draft", ""))

    def testTheLeaderThatSleepsCanBeStartedNow(self):
        swarm = Swarm("Test mission")
        leader = self.script(TimedDraftLoop(FakeAgent(["leader draft"]), 60), ["yes"])
        swarm.addAgent("Leader", leader, "boss", "finish")
        thread, outcome = self.runInThread(swarm)
        for attempt in range(300):
            if swarm.getInfo("Leader")["startAt"]:
                break
            threading.Event().wait(0.01)
        swarm.startNow("Leader")
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive(), "The leader did not start when it was told to.")
        self.assertEqual(outcome["result"], "leader draft")

    def testAnAgentThatIsNotWaitingForATimeCannotBeStartedNow(self):
        swarm = self.makeSwarm(Writer=self.worker("text"))
        for name in ("Writer", "Leader"):
            with self.assertRaisesRegex(ValueError, "not waiting for a time"):
                swarm.startNow(name)
        with self.assertRaisesRegex(ValueError, "no agent called Ghost"):
            swarm.startNow("Ghost")

    def testTheTimeOfAnAgentIsOnlyUsedWhenTheSwarmExecutes(self):
        swarm = Swarm("Test mission")
        leader = self.script(DraftLoop(FakeAgent(["leader plan", "Summary: Leader Late"])), ["yes"])
        late = self.script(TimedDraftLoop(FakeAgent(["late plan"]), 3600), [])
        swarm.addAgent("Leader", leader, "boss", "finish")
        swarm.addAgent("Late", late, "role", "task")
        swarm.setMode("plan")
        self.assertIn("Late", swarm.run())
        self.assertEqual(swarm.getInfo("Late")["plan"], "late plan")

    def testASwarmOfOneAgentShowsItsDraftInsteadOfASummaryOfIt(self):
        for mode, word in (("execute", "result"), ("plan", "plan")):
            swarm = self.reviewedSwarm(mode, ["the only draft"], ["yes"])
            leader = self.agentOf(swarm, "Leader")
            result = swarm.run()
            self.assertEqual(result, "the only draft")
            self.assertEqual(len(leader.agent.prompts), 1, mode)
            self.assertIn(f"Summary of the {word}s", self.saidText(leader))
            self.assertIn("- Leader (boss): waiting for the user: the only draft", self.saidText(leader))
            self.assertNotIn("could not write the summary", self.saidText(leader))

    def testADraftThatFailedAnAutomaticCheckIsNeverHiddenInTheSummary(self):
        swarm = self.reviewedSwarm("execute", ["Summary one: Writer Checker", "leader draft", "Summary two: Writer Checker Leader"], ["yes", "yes"],
                                   Writer=["a draft that is too long", "short"], Checker=["fine draft"])
        writer = self.agentOf(swarm, "Writer")
        writer.run = lambda: writer.reviewLoop("Do the task of Writer", lambda draft: "The text is too long." if "too long" in draft else "", ask=True)
        writer.numberOfLoops = 1
        leader, questions = self.agentOf(swarm, "Leader"), []
        ask = leader.askUser
        leader.askUser = lambda question: questions.append(question) or ask(question)
        swarm.run()
        said = self.saidText(leader)
        self.assertIn("Warning for Writer: the automatic checks found a problem with its draft: The text is too long.", said)
        self.assertNotIn("Warning for Checker", said)
        self.assertIn("- Writer (role Writer): waiting for the user (WARNING, the automatic checks found a problem: The text is too long.): a draft that is too long",
                      leader.agent.prompts[0])
        self.assertIn("- Checker (role Checker): waiting for the user: fine draft", leader.agent.prompts[0])

    def testASwarmOfOneAgentIsAskedASimplerQuestion(self):
        swarm = self.reviewedSwarm("execute", ["the only draft"], ["yes"])
        leader, questions = self.agentOf(swarm, "Leader"), []
        ask = leader.askUser
        leader.askUser = lambda question: questions.append(question) or ask(question)
        swarm.run()
        self.assertEqual(questions, ["Do you approve? Type yes to approve, no to reject, or write what you want changed:"])
        swarm = self.reviewedSwarm("execute", ["Summary one: Writer", "leader draft", "Summary two: Writer Leader"], ["yes", "yes"], Writer=["writer draft"])
        leader, questions = self.agentOf(swarm, "Leader"), []
        ask = leader.askUser
        leader.askUser = lambda question: questions.append(question) or ask(question)
        swarm.run()
        self.assertTrue(all("the name of an agent to look at it alone" in question for question in questions))

    def testTheSummaryOfTheLeaderDoesNotCarryTheRulesOrThePlanOfItsOwnTask(self):
        swarm = self.reviewedSwarm("execute", ["Summary one: Writer", "leader draft", "Summary two: Writer Leader"], ["yes", "yes"], Writer=["writer draft"])
        leader = self.agentOf(swarm, "Leader")
        leader.rules, leader.approvedPlan = "RULE OF MY TASK", "PLAN OF MY TASK"
        swarm.run()
        first, own, last = leader.agent.prompts
        for management in (first, last):
            self.assertNotIn("RULE OF MY TASK", management)
            self.assertNotIn("PLAN OF MY TASK", management)
            self.assertIn("Where each agent stands", management)
        self.assertIn("RULE OF MY TASK", own)
        self.assertIn("PLAN OF MY TASK", own)

    def testThePlanOfTheOrderWithTheLeaderDoesNotCarryTheRulesEither(self):
        swarm = self.planSwarm(['{"Writer": [], "Formatter": ["Writer"], "Checker": []}'], ["yes"])
        leader = self.agentOf(swarm, "Leader")
        leader.rules = "RULE OF MY TASK"
        self.assertTrue(swarm.planWithLeader())
        self.assertNotIn("RULE OF MY TASK", leader.agent.prompts[0])


class ManagementCallTests(LoopTestCase):
    def testTheCallsThatManageASwarmSkipTheRulesAndThePlanOfTheOwnTask(self):
        loop = Loop(FakeAgent(["own", "managing", "own again"]), rulesFile="EMAIL_RULES.md")
        loop.approvedPlan = "PLAN OF MY TASK"
        loop.receive("Writer", "a report")
        loop.receiveFromUser("be brief")
        loop.askAgent("do my task")
        loop.askAgent("summarise the swarm", own=False)
        own, managing = loop.agent.prompts
        self.assertTrue(own.startswith("Follow these rules strictly:") and "PLAN OF MY TASK" in own)
        self.assertNotIn("Follow these rules strictly", managing)
        self.assertNotIn("PLAN OF MY TASK", managing)
        self.assertTrue(all(text in managing for text in ("Writer: a report", "- be brief", "summarise the swarm")))
        loop = self.script(Loop(FakeAgent(["draft"]), rulesFile="EMAIL_RULES.md"), [])
        self.assertEqual(loop.reviewLoop("task", lambda draft: "", ask=False, own=False), "draft")
        self.assertNotIn("Follow these rules strictly", loop.agent.prompts[0])


class TaskDescriptionTests(LoopTestCase):
    def testEveryLoopDescribesItsTaskInASentence(self):
        document = self.folder / "paper.tex"
        document.write_text("Theorem 1. A statement.")
        loops = {
            "email": EmailLoop(FakeAgent(), "me@example.com", "sara@example.com", "Meeting", "move it to Friday", "French"),
            "calendar": CalendarLoop(FakeAgent(), "dentist next Monday at 10"),
            "news": NewsLoop(FakeAgent(), topics="science", outlets=("BBC World", "Nature"), maxWords=120, language="German"),
            "author": AuthorLoop(FakeAgent(), "the sea", 300),
            "literature": LiteratureSurveyLoop(FakeAgent(), "graphs", 400, searches=("arXiv",), publishers={"IEEE": 263}),
            "format": DocumentFormatLoop(FakeAgent(), document, "IEEE"),
            "math": MathCheckLoop(FakeAgent(), document),
            "coder": CoderLoop(FakeAgent(), "sort a list", self.folder / "sort.py", ["python", "-m", "pytest"]),
        }
        expected = {"email": ["French", "sara@example.com", "Meeting", "move it to Friday", "me@example.com"], "calendar": ["dentist next Monday at 10"],
                    "news": ["German", "120", "science", "BBC World, Nature"], "author": ["the sea", "300"],
                    "literature": ["graphs", "400", "arXiv", "papers of IEEE"], "format": ["paper.tex", "IEEE"], "math": ["paper.tex"],
                    "coder": ["sort a list", "sort.py", "python -m pytest"]}
        for name, loop in loops.items():
            description = loop.describeTask()
            self.assertTrue(description.endswith((".", "pytest")) or description, name)
            self.assertTrue(all(word in description for word in expected[name]), (name, description))
        self.assertEqual((Loop(FakeAgent()).describeTask(), Loop(FakeAgent()).startTime()), ("", None))


class EmailSetupTests(LoopTestCase):
    def testAWorkingLoginForSendingAndReadingIsAccepted(self):
        with mock.patch.object(harness_utils.smtplib, "SMTP") as smtp, mock.patch.object(harness_utils.imaplib, "IMAP4_SSL") as imap:
            self.assertEqual(harness_utils.checkEmailLogin("me@example.com", "pw", "smtp.test", "imap.test"), "")
        smtp.assert_called_with("smtp.test", 587, timeout=harness_utils.FETCH_TIMEOUT)
        smtp.return_value.__enter__.return_value.starttls.assert_called_once()
        smtp.return_value.__enter__.return_value.login.assert_called_with("me@example.com", "pw")
        imap.assert_called_with("imap.test", timeout=harness_utils.FETCH_TIMEOUT)
        imap.return_value.__enter__.return_value.login.assert_called_with("me@example.com", "pw")

    def testNoImapServerMeansTheInboxIsNotTested(self):
        with mock.patch.object(harness_utils.smtplib, "SMTP"), mock.patch.object(harness_utils.imaplib, "IMAP4_SSL") as imap:
            self.assertEqual(harness_utils.checkEmailLogin("me@example.com", "pw", "smtp.test"), "")
        imap.assert_not_called()

    def testEachWayToFailIsExplained(self):
        smtp = mock.patch.object(harness_utils.smtplib, "SMTP")
        with smtp as started:
            started.return_value.__enter__.return_value.login.side_effect = harness_utils.smtplib.SMTPAuthenticationError(535, b"bad credentials")
            message = harness_utils.checkEmailLogin("me@example.com", "wrong", "smtp.test")
        self.assertIn("refused the address or the password", message)
        self.assertIn("app password", message)
        with mock.patch.object(harness_utils.smtplib, "SMTP", side_effect=OSError("Name or service not known")):
            self.assertIn("Could not send through smtp.test: Name or service not known", harness_utils.checkEmailLogin("me@example.com", "pw", "smtp.test"))
        with mock.patch.object(harness_utils.smtplib, "SMTP"), mock.patch.object(harness_utils.imaplib, "IMAP4_SSL", side_effect=OSError("timed out")):
            message = harness_utils.checkEmailLogin("me@example.com", "pw", "smtp.test", "imap.test")
        self.assertIn("Sending works, but imap.test could not be used to read the replies: timed out", message)
        with mock.patch.object(harness_utils.smtplib, "SMTP"), mock.patch.object(harness_utils.imaplib, "IMAP4_SSL") as imap:
            imap.return_value.__enter__.return_value.login.side_effect = harness_utils.imaplib.IMAP4.error("LOGIN failed")
            self.assertIn("LOGIN failed", harness_utils.checkEmailLogin("me@example.com", "pw", "smtp.test", "imap.test"))

    def testTheImapServerOfTheSettingsIsUsedWithoutAnEnvironmentVariable(self):
        loop = self.script(EmailLoop(FakeAgent(), "me@example.com", "sara@example.com", "Meeting", "x"), [])
        loop.settings.update(EMAIL_PASSWORD="pw", EMAIL_IMAP_SERVER="imap.settings")
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(harness_utils.imaplib, "IMAP4_SSL") as imap:
            inbox = imap.return_value.__enter__.return_value
            inbox.search.return_value = ("OK", [b""])
            self.assertEqual(loop.fetchLatestEmail(), "")
        imap.assert_called_with("imap.settings")
        inbox.login.assert_called_with("me@example.com", "pw")
        loop = self.script(EmailLoop(FakeAgent(), "me@example.com", "sara@example.com", "Meeting", "x"), [])
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(loop.fetchLatestEmail(), "")
        self.assertIn("EMAIL_IMAP_SERVER is not set", self.saidText(loop))


class NewsScheduleTests(LoopTestCase):
    def clock(self, offsetHours):
        return f"{datetime.now() + timedelta(hours=offsetHours):%H:%M}"

    def testTheNextTimeIsTodayIfItIsStillToComeAndTomorrowIfNot(self):
        ahead = (harness_utils.nextOccurrence(self.clock(1)) - datetime.now()).total_seconds()
        behind = (harness_utils.nextOccurrence(self.clock(-1)) - datetime.now()).total_seconds()
        self.assertTrue(3500 < ahead < 3700, ahead)
        self.assertTrue(82700 < behind < 82900, behind)
        moment = harness_utils.nextOccurrence(" 07:30 ")
        self.assertEqual((moment.hour, moment.minute, moment.second), (7, 30, 0))
        self.assertGreater(moment, datetime.now())

    def testBadTimesAreExplained(self):
        for bad in ("7h30", "25:00", "12:75", "", "noon", "12:30:10"):
            with self.assertRaisesRegex(ValueError, "HH:MM, like 07:30"):
                harness_utils.nextOccurrence(bad)
            if bad:
                with self.assertRaises(ValueError):
                    NewsLoop(FakeAgent(), collectAt=bad)

    def testTheBriefingWaitsForItsTimeOnlyIfItHasOne(self):
        self.assertIsNone(NewsLoop(FakeAgent()).startTime())
        loop = NewsLoop(FakeAgent(), collectAt=self.clock(1))
        self.assertTrue(3500 < (loop.startTime() - datetime.now()).total_seconds() < 3700)
        fixed = datetime(2031, 5, 17, 6, 45)
        loop = NewsLoop(FakeAgent(), collectAt=fixed)
        self.assertEqual(loop.startTime(), fixed)
        self.assertIn("collected on 2031-05-17 at 06:45", loop.describeTask())
        self.assertNotIn("collected on", NewsLoop(FakeAgent()).describeTask())


class PublisherTests(LoopTestCase):
    def fetching(self, body, seen=None):
        def fetch(url, login=None, limit=harness_utils.MAX_DOWNLOAD):
            if seen is not None:
                seen.append(url)
            return body.encode()
        return mock.patch.object(harness_utils, "fetchUrl", fetch)

    def testASearchInAPublisherOnlyAsksForItsPapers(self):
        seen = []
        body = json.dumps({"message": {"items": [{"title": ["A paper"], "abstract": "<p>Abstract</p>", "URL": "https://doi.org/10.1038/x"}]}})
        with self.fetching(body, seen):
            papers = harness_utils.searchCrossref("graphs", 297)
            harness_utils.searchCrossref("graphs")
        self.assertEqual(papers, [{"title": "A paper", "summary": "Abstract", "link": "https://doi.org/10.1038/x"}])
        asked = urllib.parse.parse_qs(urllib.parse.urlparse(seen[0]).query)
        self.assertEqual(asked["filter"], ["member:297,type:journal-article,type:proceedings-article"])
        self.assertEqual(asked["query"], ["graphs"])
        self.assertNotIn("filter", urllib.parse.parse_qs(urllib.parse.urlparse(seen[1]).query))

    def testPublishersAreFoundByNameTheBiggestFirst(self):
        items = [{"id": 1, "primary-name": "Springer Global Publication", "counts": {"total-dois": 40}}, {"id": 297, "primary-name": "Springer Science and Business Media LLC", "counts": {"total-dois": 18972840}},
                 {"id": 2, "primary-name": "No count"}, {"id": 3}, {"primary-name": "No id"}]
        seen = []
        with self.fetching(json.dumps({"message": {"items": items}}), seen):
            found = harness_utils.findPublishers("  springer ")
        self.assertEqual([(publisher["name"], publisher["id"], publisher["papers"]) for publisher in found],
                         [("Springer Science and Business Media LLC", 297, 18972840), ("Springer Global Publication", 1, 40), ("No count", 2, 0)])
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlparse(seen[0]).query)["query"], ["springer"])
        with self.fetching("{}"):
            self.assertEqual(harness_utils.findPublishers("nobody"), [])

    def testTheSurveyCanBeRestrictedToPublishersAndTheyAreMergedWithTheSearches(self):
        seen = []
        def crossref(subject, publisher=None):
            seen.append((subject, publisher))
            return [{"title": f"Paper of {publisher}", "summary": "s", "link": f"https://p{publisher}.test/1"},
                    {"title": "Shared paper", "summary": "s", "link": f"https://shared{publisher}.test"}]
        with mock.patch.object(harness_utils, "searchCrossref", crossref), mock.patch.dict(harness_utils.PAPER_SEARCHES, {"arXiv": lambda subject: [{"title": "shared  PAPER", "summary": "s", "link": "https://arxiv.test/1"}]}):
            loop = self.script(LiteratureSurveyLoop(FakeAgent(), "graphs", 100, searches=("arXiv",), publishers={"IEEE": 263, "ACM": 320}), [])
            papers = loop.searchPapers()
            only = self.script(LiteratureSurveyLoop(FakeAgent(), "graphs", 100, searches=(), publishers={"IEEE": 263}), [])
            onlyPapers = only.searchPapers()
        self.assertEqual(seen, [("graphs", 263), ("graphs", 320), ("graphs", 263)])
        self.assertEqual([paper["title"] for paper in papers], ["shared  PAPER", "Paper of 263", "Paper of 320"])
        self.assertEqual([paper["title"] for paper in onlyPapers], ["Paper of 263", "Shared paper"])

    def testAFailingPublisherIsSkippedByName(self):
        def broken(subject, publisher=None):
            raise urllib.error.HTTPError("u", 429, "Too Many", {}, None)
        with mock.patch.object(harness_utils, "searchCrossref", broken):
            loop = self.script(LiteratureSurveyLoop(FakeAgent(), "graphs", 100, searches=(), publishers={"IEEE": 263}), [])
            self.assertEqual(loop.searchPapers(), [])
        self.assertIn("Skipping the search on IEEE: the website says it received too many requests", self.saidText(loop))

    def testPublisherCanBeGivenWithoutChangingTheOtherArguments(self):
        loop = LiteratureSurveyLoop(FakeAgent(), "graphs", 100)
        self.assertEqual((loop.searches, loop.publishers, loop.numberOfLoops), (tuple(harness_utils.PAPER_SEARCHES), {}, 5))
        loop = LiteratureSurveyLoop(FakeAgent(), "graphs", 100, ("arXiv",), 3, {"IEEE": 263})
        self.assertEqual((loop.searches, loop.publishers, loop.numberOfLoops), (("arXiv",), {"IEEE": 263}, 3))


class GpuReadingTests(unittest.TestCase):
    def smi(self, output="", code=0, error=None):
        run = mock.patch.object(harness_utils.subprocess, "run", side_effect=error, return_value=subprocess.CompletedProcess([], code, stdout=output, stderr=""))
        finder = mock.patch.object(harness_utils, "findNvidiaSmi", return_value="nvidia-smi")
        self.addCleanup(run.stop)
        self.addCleanup(finder.stop)
        finder.start()
        return run.start()

    def testNvidiaGpusAreReadInGb(self):
        self.smi("NVIDIA RTX A5000, 24564, 24098\nNVIDIA RTX A5000, 24564, 23915\n")
        self.assertEqual(harness_utils.readNvidiaGpus(), [{"name": "NVIDIA RTX A5000", "total": 25.8, "free": 25.3},
                                                          {"name": "NVIDIA RTX A5000", "total": 25.8, "free": 25.1}])

    def testLinesThatCannotBeReadAreSkippedAndNamesMayHaveCommas(self):
        self.smi("GPU A, 8192, 100\nGPU B, [N/A], [N/A]\nbroken\nVendor, Inc GPU, 4096, 4096\n\n")
        self.assertEqual([gpu["name"] for gpu in harness_utils.readNvidiaGpus()], ["GPU A", "Vendor, Inc GPU"])

    def testNoToolOrAFailingToolMeansNoGpu(self):
        with mock.patch.object(harness_utils, "findNvidiaSmi", return_value=None):
            self.assertEqual(harness_utils.readNvidiaGpus(), [])
        for failure in (FileNotFoundError("gone"), subprocess.TimeoutExpired("nvidia-smi", 10), PermissionError("no")):
            self.smi(error=failure)
            self.assertEqual(harness_utils.readNvidiaGpus(), [], failure)
        self.smi("NVIDIA RTX A5000, 24564, 24098\n", code=9)
        self.assertEqual(harness_utils.readNvidiaGpus(), [])

    def testTheToolIsStartedWithATimeoutAndWithoutAWindowOnWindows(self):
        run = self.smi("GPU, 1000, 1000")
        with mock.patch.object(harness_utils.subprocess, "CREATE_NO_WINDOW", 134217728, create=True):
            harness_utils.readNvidiaGpus()
        self.assertEqual(run.call_args.kwargs["creationflags"], 134217728)
        self.assertEqual(run.call_args.kwargs["timeout"], harness_utils.GPU_TIMEOUT)
        self.assertEqual(run.call_args.args[0][0], "nvidia-smi")

    def testNvidiaSmiIsFoundInTheInstallFolderOfWindowsWhenItIsNotInThePath(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        with mock.patch.object(harness_utils.shutil, "which", return_value=None), mock.patch.dict(os.environ, {"ProgramFiles": folder.name}):
            self.assertIsNone(harness_utils.findNvidiaSmi())
            tool = Path(folder.name) / "NVIDIA Corporation" / "NVSMI" / "nvidia-smi.exe"
            tool.parent.mkdir(parents=True)
            tool.write_text("")
            self.assertEqual(harness_utils.findNvidiaSmi(), str(tool))
        with mock.patch.object(harness_utils.shutil, "which", return_value="/usr/bin/nvidia-smi"):
            self.assertEqual(harness_utils.findNvidiaSmi(), "/usr/bin/nvidia-smi")

    def makeCard(self, drm, name, total="17163091968", used="1163091968", product=None):
        device = Path(drm) / name / "device"
        device.mkdir(parents=True)
        for file, value in (("mem_info_vram_total", total), ("mem_info_vram_used", used), ("product_name", product)):
            if value is not None:
                (device / file).write_text(value + "\n")

    def testAmdGpusAreReadFromTheDriverFilesWithoutCountingTheConnectorsTwice(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.makeCard(folder.name, "card0", product="AMD Radeon RX 7900 XT")
        self.makeCard(folder.name, "card0-DP-1", product="AMD Radeon RX 7900 XT")
        self.makeCard(folder.name, "card1", total=None)
        self.makeCard(folder.name, "card2", total="garbage")
        self.makeCard(folder.name, "card3", total="8589934592", used="0")
        (Path(folder.name) / "version").write_text("drm")
        with mock.patch.object(harness_utils, "DRM_FOLDER", Path(folder.name)):
            self.assertEqual(harness_utils.readAmdGpus(), [{"name": "AMD Radeon RX 7900 XT", "total": 17.2, "free": 16.0},
                                                           {"name": "AMD GPU card3", "total": 8.6, "free": 8.6}])

    def testWithoutTheLinuxFolderThereIsNoAmdGpu(self):
        with mock.patch.object(harness_utils, "DRM_FOLDER", Path("/this/folder/does/not/exist")):
            self.assertEqual(harness_utils.readAmdGpus(), [])

    def testAllTheVendorsAreJoined(self):
        with mock.patch.object(harness_utils, "readNvidiaGpus", return_value=[{"name": "N", "total": 24.0, "free": 20.0}]), \
                mock.patch.object(harness_utils, "readAmdGpus", return_value=[{"name": "A", "total": 16.0, "free": 16.0}]):
            self.assertEqual([gpu["name"] for gpu in harness_utils.queryGpus()], ["N", "A"])

    def testTheGpusAreNotReadAgainWithinAFewSeconds(self):
        with mock.patch.dict(harness_utils.gpuCache, {"loaded": 0, "gpus": []}), mock.patch.object(harness_utils, "queryGpus", return_value=[{"name": "G"}]) as query:
            self.assertEqual((readGpus(), readGpus(), readGpus()), ([{"name": "G"}],) * 3)
            self.assertEqual(query.call_count, 1)
            harness_utils.gpuCache["loaded"] -= harness_utils.GPU_REFRESH_SECONDS + 1
            readGpus()
            self.assertEqual(query.call_count, 2)

    def testWhatTheGpusCanHostIsSeparatedFromWhatIsFreeNow(self):
        gpus = [{"name": "A", "total": 24.0, "free": 10.0}, {"name": "B", "total": 24.0, "free": 24.0}]
        status = checkVram(30.0, gpus)
        self.assertEqual((status["total"], status["free"], status["used"], status["left"], status["fits"], status["runnable"]), (48.0, 34.0, 14.0, 18.0, True, True))
        self.assertEqual(status["message"], "")
        status = checkVram(40.0, gpus)
        self.assertEqual((status["fits"], status["runnable"]), (True, False))
        self.assertIn("only 34.0 GB are free now because other jobs use 14.0 GB", status["message"])
        self.assertIn("will not be able to run the swarm", status["message"])
        status = checkVram(50.0, gpus)
        self.assertEqual((status["fits"], status["runnable"]), (False, False))
        self.assertIn("only have 48.0 GB in total", status["message"])

    def testNoGpuHostsNothingButNothingIsNeededForApiModels(self):
        status = checkVram(5.0, [])
        self.assertEqual((status["fits"], status["runnable"], status["message"]), (False, False, NO_GPU_MESSAGE))
        status = checkVram(0.0, [])
        self.assertEqual((status["fits"], status["runnable"], status["message"]), (True, True, ""))


class SwarmVramTests(unittest.TestCase):
    NINE, FOUR, TWO, SMALL = (getModelInfo(f"Qwen/Qwen3.5-{size}") for size in ("9B", "4B", "2B", "0.8B"))
    API = getModelInfo("claude-opus-5-5")

    def setUp(self):
        self.assertEqual((self.NINE["vram"], self.FOUR["vram"], self.TWO["vram"], self.SMALL["vram"]), (23.3, 11.3, 5.5, 2.2))
        self.gpus = []
        patcher = mock.patch.object(harness_utils, "readGpus", lambda: self.gpus)
        patcher.start()
        self.addCleanup(patcher.stop)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patcher = mock.patch.object(harness_utils, "AGENT_FILES", Path(folder.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def useGpus(self, total, free=None):
        self.gpus[:] = [{"name": "GPU", "total": total, "free": total if free is None else free}]

    def agent(self):
        return Loop(FakeAgent())

    def swarmWith(self, **models):
        swarm = Swarm("Test mission")
        for name, model in models.items():
            swarm.addAgent(name, self.agent(), "role", "task", model=model)
        return swarm

    def testTheVramOfTheSwarmGrowsWithEachAgentAndModel(self):
        self.useGpus(80.0)
        swarm = Swarm("Test mission")
        seen = []
        for name, model in (("A", self.NINE), ("B", self.FOUR), ("C", self.API), ("D", None)):
            swarm.addAgent(name, self.agent(), "role", "task", model=model)
            seen.append(swarm.getVramStatus()["needed"])
        self.assertEqual(seen, [23.3, 34.6, 34.6, 34.6])
        status = swarm.getVramStatus()
        self.assertEqual((status["total"], status["free"], status["left"], status["fits"], status["runnable"], status["message"]), (80.0, 80.0, 45.4, True, True, ""))
        self.assertEqual(status["gpus"], self.gpus)
        self.assertEqual(swarm.getInfo("A")["model"], self.NINE)
        self.assertIsNone(swarm.getInfo("D")["model"])
        json.dumps(swarm.getInfo("A"))

    def testAModelThatDoesNotFitInTheGpusAtAllCannotBeChosen(self):
        self.useGpus(20.0)
        swarm = Swarm("Test mission")
        check = swarm.checkModel(self.NINE)
        self.assertFalse(check["allowed"])
        self.assertIn("needs about 23.3 GB of VRAM, but your GPUs only have 20.0 GB in total", check["message"])
        self.assertIn("Choose a smaller model or an API model", check["message"])
        with self.assertRaisesRegex(ValueError, "only have 20.0 GB"):
            swarm.addAgent("A", self.agent(), "role", "task", model=self.NINE)
        self.assertEqual(swarm.getAgents(), [])
        self.assertTrue(swarm.checkModel(self.FOUR)["allowed"])

    def testTheSwarmAsAWholeMustFitInTheGpus(self):
        self.useGpus(40.0)
        swarm = self.swarmWith(A=self.NINE)
        refused = swarm.checkModel(self.NINE)
        self.assertFalse(refused["allowed"])
        self.assertEqual(refused["needed"], 46.6)
        self.assertIn("The other agents already take 23.3 GB", refused["message"])
        self.assertIn("no room left", refused["message"])
        with self.assertRaises(ValueError):
            swarm.addAgent("B", self.agent(), "role", "task", model=self.NINE)
        swarm.addAgent("B", self.agent(), "role", "task", model=self.FOUR)
        self.assertEqual(swarm.getNeededVram(), 34.6)
        self.assertFalse(swarm.checkModel(self.TWO)["allowed"])
        self.assertTrue(swarm.checkModel(self.SMALL)["allowed"])
        swarm.addAgent("C", self.agent(), "role", "task", model=self.SMALL)
        self.assertEqual((swarm.getNeededVram(), swarm.getVramStatus()["left"]), (36.8, 3.2))

    def testApiModelsCanAlwaysBeChosenEvenWhenTheGpusAreFull(self):
        self.useGpus(24.0)
        swarm = self.swarmWith(A=self.NINE)
        self.assertEqual(swarm.getVramStatus()["left"], 0.7)
        self.assertFalse(swarm.checkModel(self.SMALL)["allowed"])
        swarm.addAgent("B", self.agent(), "role", "task", model=self.API)
        self.assertEqual((swarm.getNeededVram(), swarm.getVramStatus()["left"]), (23.3, 0.7))
        self.assertFalse(swarm.checkModel(self.SMALL)["allowed"])

    def testApiModelsStillWorkWhenAGpuGoesAwayAndTheSwarmNoLongerFits(self):
        self.useGpus(48.0)
        swarm = self.swarmWith(A=self.NINE, B=self.NINE)
        self.useGpus(24.0)
        status = swarm.getVramStatus()
        self.assertEqual((status["fits"], status["runnable"]), (False, False))
        self.assertIn("only have 24.0 GB in total", status["message"])
        self.assertTrue(swarm.checkModel(self.API)["allowed"])
        swarm.addAgent("C", self.agent(), "role", "task", model=self.API)
        self.assertFalse(swarm.checkModel(self.SMALL)["allowed"])
        swarm.setModel("B", self.API)
        self.assertTrue(swarm.getVramStatus()["fits"])

    def testAModelThatFitsButIsUsedByOtherJobsIsAllowedWithAWarning(self):
        self.useGpus(40.0, free=10.0)
        swarm = Swarm("Test mission")
        check = swarm.checkModel(self.NINE)
        self.assertTrue(check["allowed"])
        self.assertIn("only 10.0 GB are free now because other jobs use 30.0 GB", check["message"])
        self.assertIn("will not be able to run the swarm", check["message"])
        swarm.addAgent("A", self.agent(), "role", "task", model=self.NINE)
        status = swarm.getVramStatus()
        self.assertEqual((status["fits"], status["runnable"]), (True, False))
        self.assertIn("other jobs use 30.0 GB", status["message"])
        self.assertIn("other jobs use 30.0 GB", swarm.checkModel(self.API)["message"])
        self.assertTrue(swarm.checkModel(self.API)["allowed"])

    def testTheWarningDisappearsWhenTheOtherJobsFinish(self):
        self.useGpus(40.0, free=10.0)
        swarm = self.swarmWith(A=self.NINE)
        self.assertFalse(swarm.getVramStatus()["runnable"])
        self.useGpus(40.0, free=40.0)
        status = swarm.getVramStatus()
        self.assertEqual((status["runnable"], status["message"]), (True, ""))

    def testNoGpuMeansNoLocalModelButApiModelsStillWork(self):
        swarm = Swarm("Test mission")
        check = swarm.checkModel(self.FOUR)
        self.assertEqual((check["allowed"], check["message"]), (False, NO_GPU_MESSAGE))
        self.assertTrue(swarm.checkModel(self.API)["allowed"])
        swarm.addAgent("A", self.agent(), "role", "task", model=self.API)
        self.assertEqual(swarm.getVramStatus()["gpus"], [])

    def testChangingToASmallerModelMakesRoomAndABiggerOneIsStillChecked(self):
        self.useGpus(40.0)
        swarm = self.swarmWith(A=self.NINE, B=self.FOUR)
        newLoop = self.agent()
        swarm.setModel("A", self.TWO, agent=newLoop)
        self.assertEqual((swarm.getNeededVram(), swarm.getInfo("A")["model"]), (16.8, self.TWO))
        self.assertIs(swarm.members["A"]["agent"], newLoop)
        swarm.setModel("B", self.NINE)
        self.assertEqual(swarm.getNeededVram(), 28.8)
        with self.assertRaisesRegex(ValueError, "no room left"):
            swarm.setModel("A", self.NINE)
        self.assertEqual((swarm.getNeededVram(), swarm.getInfo("A")["model"]), (28.8, self.TWO))
        self.assertIs(swarm.members["A"]["agent"], newLoop)
        swarm.setModel("B", self.API)
        self.assertEqual(swarm.getNeededVram(), 5.5)
        with self.assertRaises(ValueError):
            swarm.setModel("Ghost", self.TWO)

    def testTheModelBeingReplacedDoesNotCountAgainstItself(self):
        self.useGpus(24.0)
        swarm = self.swarmWith(A=self.NINE)
        self.assertTrue(swarm.checkModel(self.NINE, replacing="A")["allowed"])
        self.assertFalse(swarm.checkModel(self.NINE)["allowed"])

    def testTheSwarmDoesNotRunWhileTheGpusAreBusyAndRunsWhenTheyAreFree(self):
        self.useGpus(40.0, free=10.0)
        worker, leader = self.agent(), self.agent()
        ran = []
        worker.run = lambda: ran.append("worker") or "text"
        leader.run = lambda: ran.append("leader") or "final"
        swarm = Swarm("Test mission")
        swarm.addAgent("Leader", leader, "boss", "finish", model=self.API)
        swarm.addAgent("Worker", worker, "worker", "work", model=self.NINE)
        with self.assertRaisesRegex(ValueError, "will not be able to run the swarm"):
            swarm.run()
        self.assertEqual((ran, set(swarm.getStatuses().values())), ([], {"waiting"}))
        self.useGpus(40.0, free=30.0)
        self.assertEqual(swarm.run(), "final")
        self.assertEqual(ran, ["worker", "leader"])

    def testPlanModeNeedsTheGpusToo(self):
        self.useGpus(40.0, free=10.0)
        swarm = self.swarmWith(Leader=self.NINE)
        swarm.setMode("plan")
        with self.assertRaisesRegex(ValueError, "other jobs use 30.0 GB"):
            swarm.run()

    def testTheGpusAreNotReadWhenNoLocalModelIsUsed(self):
        with mock.patch.object(harness_utils, "readGpus", side_effect=AssertionError("the GPUs must not be read")):
            swarm = Swarm("Test mission")
            leader = self.agent()
            leader.run = lambda: "final"
            swarm.addAgent("Leader", leader, "boss", "finish", model=self.API)
            swarm.addAgent("Other", self.agent(), "role", "task")
            self.assertEqual(swarm.checkModel(self.API)["needed"], 0.0)
            swarm.getStages()
            swarm.checkGpus()


# A pretend Telegram and WhatsApp. mode says how they answer, requests keeps what was asked, and updates is what the bot of Telegram received.
# do_GET, do_POST and log_message are the names the standard library requires.
class FakeMessengers(BaseHTTPRequestHandler):
    def log_message(self, *arguments):
        pass

    def reply(self, code, body):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        self.server.requests.append({"method": self.command, "path": self.path, "headers": {key.lower(): value for key, value in self.headers.items()}, "body": body})
        mode = self.server.mode
        if self.path.startswith("/telegram/"):
            method = self.path.rsplit("/", 1)[-1]
            failures = {"401": (401, "Unauthorized"), "nochat": (400, "Bad Request: chat not found"), "blocked": (403, "Forbidden: bot was blocked by the user"),
                        "429": (429, "Too Many Requests: retry after 5"), "other": (409, "Conflict: a webhook is active")}
            if mode in failures and not (mode == "nochat" and method not in ("sendMessage", "getChat")):
                code, description = failures[mode]
                return self.reply(code, {"ok": False, "error_code": code, "description": description})
            return self.reply(200, {"ok": True, "result": self.server.updates if method == "getUpdates" else {"id": 1}})
        errors = {"window": (400, 131047, "Re-engagement message"), "token": (401, 190, "Error validating access token"), "limit": (429, 130429, "Rate limit hit"),
                  "other": (400, 131030, "Recipient phone number not in allowed list")}
        if mode in errors:
            code, number, message = errors[mode]
            return self.reply(code, {"error": {"message": f"(#{number}) {message}", "code": number, "error_data": {"messaging_product": "whatsapp", "details": message}}})
        if mode == "html":
            return self.reply(502, b"<html>Bad gateway</html>")
        return self.reply(200, {"messaging_product": "whatsapp", "messages": [{"id": "wamid.1"}]})

    do_GET = do_POST = answer


class MessagingTests(unittest.TestCase):
    TELEGRAM = {"token": "123:SECRET-TOKEN", "chat": "42"}
    WHATSAPP = {"token": "WA-SECRET", "phoneId": "555", "to": "+4915112345678"}

    @classmethod
    def setUpClass(cls):
        os.environ["no_proxy"] = "127.0.0.1,localhost"
        os.environ["NO_PROXY"] = "127.0.0.1,localhost"
        cls.server = HTTPServer(("127.0.0.1", 0), FakeMessengers)
        cls.server.requests, cls.server.mode, cls.server.updates = [], "ok", []
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.server.requests.clear()
        self.server.mode, self.server.updates = "ok", []
        for name, place in (("TELEGRAM_API", "/telegram"), ("WHATSAPP_API", "/whatsapp")):
            patcher = mock.patch.object(harness_utils, name, self.base + place)
            patcher.start()
            self.addCleanup(patcher.stop)

    def testTelegramGetsTheTextAndTheChat(self):
        sendMessage("Telegram", self.TELEGRAM, "Hello world")
        request = self.server.requests[0]
        self.assertEqual((request["method"], request["path"]), ("POST", "/telegram/bot123:SECRET-TOKEN/sendMessage"))
        self.assertEqual(request["body"], {"chat_id": "42", "text": "Hello world"})
        self.assertEqual(request["headers"]["content-type"], "application/json")

    def testWhatsappGetsTheTextAndTheTokenOnlyInTheHeader(self):
        sendMessage("WhatsApp", self.WHATSAPP, "Hello world")
        request = self.server.requests[0]
        self.assertEqual((request["method"], request["path"]), ("POST", "/whatsapp/555/messages"))
        self.assertEqual(request["headers"]["authorization"], "Bearer WA-SECRET")
        self.assertEqual(request["body"], {"messaging_product": "whatsapp", "recipient_type": "individual", "to": "+4915112345678", "type": "text",
                                           "text": {"preview_url": False, "body": "Hello world"}})

    def testSpacesAroundTheTokenAreForgiven(self):
        sendMessage("Telegram", {"token": "  123:SECRET-TOKEN\n", "chat": " 42 "}, "Hi")
        self.assertEqual(self.server.requests[0]["path"], "/telegram/bot123:SECRET-TOKEN/sendMessage")
        self.assertEqual(self.server.requests[0]["body"]["chat_id"], "42")

    def testALongBriefingIsSentInPartsAndNothingIsLost(self):
        text = "\n".join(f"Story {number}: " + "word " * 40 for number in range(60))
        sendMessage("Telegram", self.TELEGRAM, text)
        parts = [request["body"]["text"] for request in self.server.requests]
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(part) <= harness_utils.MESSAGE_LIMIT for part in parts))
        self.assertEqual(" ".join(" ".join(parts).split()), " ".join(text.split()))
        self.assertTrue(all(part.startswith("Story") or part.startswith("word") for part in parts))

    def testEveryProblemIsExplainedAndNeverShowsTheToken(self):
        cases = [("Telegram", "401", "refused the bot token"), ("Telegram", "nochat", "does not know this chat"), ("Telegram", "blocked", "not allowed to write"),
                 ("Telegram", "429", "too many messages"), ("Telegram", "other", "error 409: Conflict: a webhook is active"),
                 ("WhatsApp", "window", "wrote to the app in the last 24 hours"), ("WhatsApp", "token", "refused the access token"),
                 ("WhatsApp", "limit", "too many messages"), ("WhatsApp", "other", "error 131030: Recipient phone number not in allowed list"),
                 ("WhatsApp", "html", "error 502")]
        for app, mode, words in cases:
            self.server.mode = mode
            with self.assertRaises(MessagingError) as caught:
                sendMessage(app, self.TELEGRAM if app == "Telegram" else self.WHATSAPP, "x")
            self.assertIn(words, str(caught.exception), (app, mode))
            for secret in ("SECRET-TOKEN", "WA-SECRET", "123:"):
                self.assertNotIn(secret, str(caught.exception), (app, mode))

    def testCheckingTheInformationSendsNothing(self):
        self.assertEqual(checkMessenger("Telegram", self.TELEGRAM), "")
        self.assertEqual([request["path"].rsplit("/", 1)[-1] for request in self.server.requests], ["getMe", "getChat"])
        self.assertEqual(self.server.requests[1]["body"], {"chat_id": "42"})
        self.server.requests.clear()
        self.assertEqual(checkMessenger("WhatsApp", self.WHATSAPP), "")
        self.assertEqual([(request["method"], request["path"]) for request in self.server.requests], [("GET", "/whatsapp/555?fields=display_phone_number")])
        self.server.mode = "401"
        self.assertIn("refused the bot token", checkMessenger("Telegram", self.TELEGRAM))
        self.server.mode = "token"
        self.assertIn("refused the access token", checkMessenger("WhatsApp", self.WHATSAPP))
        self.assertIn("is not one of the messaging apps", checkMessenger("Signal", {}))

    def testTheChatsThatWroteToTheBotAreFoundNewestFirst(self):
        self.server.updates = [{"update_id": 1, "message": {"chat": {"id": 7, "first_name": "Old", "last_name": "Friend"}}},
                               {"update_id": 2, "message": {"chat": {"id": 42, "first_name": "Sara"}}},
                               {"update_id": 3, "channel_post": {"chat": {"id": -100, "title": "News channel"}}},
                               {"update_id": 4, "message": {"chat": {"id": 42, "first_name": "Sara"}}},
                               {"update_id": 5, "poll": {}}, {"update_id": 6, "message": {"chat": {"id": 9, "username": "bobby"}}}]
        self.assertEqual(findTelegramChats("123:SECRET-TOKEN"), [{"id": 9, "name": "bobby"}, {"id": 42, "name": "Sara"}, {"id": -100, "name": "News channel"},
                                                                  {"id": 7, "name": "Old Friend"}])
        self.server.updates = []
        self.assertEqual(findTelegramChats("123:SECRET-TOKEN"), [])
        self.server.mode = "401"
        with self.assertRaisesRegex(MessagingError, "refused the bot token"):
            findTelegramChats("123:SECRET-TOKEN")

    def testALostInternetIsALostConnectionAndAnUnreachableAppIsAnError(self):
        with mock.patch.object(harness_utils, "TELEGRAM_API", "http://127.0.0.1:1"):
            with mock.patch.object(harness_utils, "isOnline", lambda: False):
                with self.assertRaises(ConnectionLost):
                    sendMessage("Telegram", self.TELEGRAM, "x")
                self.assertIn("connection was lost", checkMessenger("Telegram", self.TELEGRAM))
            with mock.patch.object(harness_utils, "isOnline", lambda: True):
                with self.assertRaisesRegex(MessagingError, "Telegram could not be reached"):
                    sendMessage("Telegram", self.TELEGRAM, "x")

    def testNothingToSendAndUnknownAppsAreRefused(self):
        with self.assertRaisesRegex(MessagingError, "nothing to send"):
            sendMessage("Telegram", self.TELEGRAM, "  \n ")
        with self.assertRaisesRegex(MessagingError, "not one of the messaging apps"):
            sendMessage("Signal", {}, "x")
        self.assertEqual(self.server.requests, [])

    def testTheListOfTheUserAndTheSendersHaveTheSameApps(self):
        self.assertEqual(set(MESSAGING_APPS), set(harness_utils.MESSENGERS))
        self.assertEqual(set(MESSAGING_APPS), set(harness_utils.MESSENGER_CHECKS))
        for app, details in MESSAGING_APPS.items():
            self.assertTrue(details["info"].strip() and details["fields"], app)
            self.assertTrue(all(field["ask"].strip() and field["kind"] in ("secret", "text", "phone") for field in details["fields"]), app)
        self.assertIn("24 hours", MESSAGING_APPS["WhatsApp"]["info"])


class SplitMessageTests(unittest.TestCase):
    def testShortTextsAreNotCut(self):
        self.assertEqual(splitMessage("  hello  "), ["hello"])
        self.assertEqual(splitMessage(""), [])
        self.assertEqual(splitMessage("x" * 10, limit=10), ["x" * 10])

    def testALongTextIsCutAtTheEndOfALineThenAtASpaceThenAnywhere(self):
        self.assertEqual(splitMessage("aaaa bbbb\ncccc dddd", limit=12), ["aaaa bbbb", "cccc dddd"])
        self.assertEqual(splitMessage("aaaa bbbb cccc", limit=11), ["aaaa bbbb", "cccc"])
        self.assertEqual(splitMessage("x" * 25, limit=10), ["x" * 10, "x" * 10, "x" * 5])
        self.assertEqual(splitMessage("ab" + " " + "c" * 20, limit=10), ["ab ccccccc", "cccccccccc", "ccc"])


def failing(error):
    raise error


class ConnectionTests(LoopTestCase):
    def testTheConnectionIsAskedFromTheProxyFirstThenFromPublicServers(self):
        tried = []
        def connect(target, timeout):
            tried.append(target)
            raise OSError("no")
        proxies = {"http": "http://proxy.test:3128", "https": "https://secure-proxy.test", "no": "localhost"}
        with mock.patch.object(harness_utils.urllib.request, "getproxies", lambda: proxies), mock.patch.object(harness_utils.socket, "create_connection", connect):
            self.assertFalse(harness_utils.isOnline())
        self.assertEqual(tried, [("proxy.test", 3128), ("secure-proxy.test", 443), *harness_utils.CONNECTION_HOSTS])

    def testOneAnswerIsEnough(self):
        class Connection:
            def close(self):
                pass
        tried = []
        with mock.patch.object(harness_utils.urllib.request, "getproxies", lambda: {}), \
                mock.patch.object(harness_utils.socket, "create_connection", lambda target, timeout: tried.append(target) or Connection()):
            self.assertTrue(harness_utils.isOnline())
        self.assertEqual(tried, [harness_utils.CONNECTION_HOSTS[0]])

    def testOnlyARealOutageIsALostConnection(self):
        with mock.patch.object(harness_utils, "isOnline", lambda: False):
            lost = ConnectionLost("gone")
            self.assertIs(harness_utils.asConnectionLost(lost), lost)
            self.assertIsInstance(harness_utils.asConnectionLost(OSError("unreachable")), ConnectionLost)
            self.assertIsNone(harness_utils.asConnectionLost(urllib.error.HTTPError("http://x.test", 500, "boom", {}, None)))
            self.assertIsNone(harness_utils.asConnectionLost(harness_utils.smtplib.SMTPAuthenticationError(535, b"refused")))
            self.assertIsNone(harness_utils.asConnectionLost(ValueError("bad")))
        with mock.patch.object(harness_utils, "isOnline", lambda: True):
            self.assertIsNone(harness_utils.asConnectionLost(OSError("this one website is down")))

    def testRaiseIfOfflineTurnsTheErrorIntoALostConnectionOnlyWhenOffline(self):
        error = OSError("unreachable")
        with mock.patch.object(harness_utils, "isOnline", lambda: False), self.assertRaises(ConnectionLost) as caught:
            harness_utils.raiseIfOffline(error)
        self.assertIs(caught.exception.__cause__, error)
        with mock.patch.object(harness_utils, "isOnline", lambda: True):
            harness_utils.raiseIfOffline(error)

    def makeLoop(self):
        loop = Loop(FakeAgent())
        loop.waited = []
        loop.onConnectionLost = loop.waited.append
        return loop

    def testAStepIsTriedAgainEachTimeTheUserDecidesToContinue(self):
        loop, calls = self.makeLoop(), []
        def step():
            calls.append(1)
            if len(calls) < 3:
                raise ConnectionLost("gone")
            return "fine"
        self.assertEqual(loop.keepTrying(step), "fine")
        self.assertEqual((len(calls), [str(error) for error in loop.waited]), (3, ["gone", "gone"]))

    def testANetworkErrorWaitsOnlyIfTheInternetIsReallyGone(self):
        loop, calls = self.makeLoop(), []
        def step():
            calls.append(1)
            if len(calls) == 1:
                raise OSError("unreachable")
            return "fine"
        with mock.patch.object(harness_utils, "isOnline", lambda: False):
            self.assertEqual(loop.keepTrying(step), "fine")
        self.assertEqual(len(loop.waited), 1)
        self.assertIsInstance(loop.waited[0], ConnectionLost)
        calls.clear(), loop.waited.clear()
        with mock.patch.object(harness_utils, "isOnline", lambda: True), self.assertRaises(OSError):
            loop.keepTrying(step)
        self.assertEqual(loop.waited, [])

    def testStoppingTheSwarmEndsTheWaitingStep(self):
        loop = Loop(FakeAgent())
        loop.onConnectionLost = lambda error: failing(harness_utils.SwarmStopped("stop"))
        with self.assertRaises(harness_utils.SwarmStopped):
            loop.keepTrying(lambda: failing(ConnectionLost("gone")))

    def testWithoutASwarmNothingChangesAndNothingIsAsked(self):
        loop = Loop(FakeAgent())
        with mock.patch.object(harness_utils, "isOnline", side_effect=AssertionError("the connection must not be asked")):
            with self.assertRaises(OSError):
                loop.keepTrying(lambda: failing(OSError("down")))
            with self.assertRaises(ConnectionLost):
                loop.keepTrying(lambda: failing(ConnectionLost("gone")))
            loop.checkOnline("reading")

    def testAnEmptyResultIsALostConnectionOnlyInsideASwarmWhenTheInternetIsGone(self):
        loop = self.makeLoop()
        with mock.patch.object(harness_utils, "isOnline", lambda: False), self.assertRaisesRegex(ConnectionLost, "lost while reading"):
            loop.checkOnline("reading")
        with mock.patch.object(harness_utils, "isOnline", lambda: True):
            loop.checkOnline("reading")

    def testTheCallsThatManageTheSwarmNeverWaitButTheOwnCallsDo(self):
        agent = FakeAgent()
        agent.input = lambda prompt: failing(ConnectionLost("gone"))
        loop = self.makeLoop()
        loop.agent = agent
        with self.assertRaises(ConnectionLost):
            loop.askAgent("manage", own=False)
        self.assertEqual(loop.waited, [])
        loop.onConnectionLost = lambda error: failing(harness_utils.SwarmStopped("stop"))
        with self.assertRaises(harness_utils.SwarmStopped):
            loop.askAgent("work")

    def testNoNewsMeansALostConnectionWhenTheInternetIsGone(self):
        loop = NewsLoop(FakeAgent(), outlets=["https://dead.test/feed"])
        loop.said = []
        loop.notifyUser = loop.said.append
        loop.onConnectionLost = lambda error: failing(harness_utils.SwarmStopped("stop"))
        def dead(url, limit=10, discover=True):
            raise urllib.error.URLError("unreachable")
        with mock.patch.object(harness_utils, "readFeed", dead):
            with mock.patch.object(harness_utils, "isOnline", lambda: False), self.assertRaises(harness_utils.SwarmStopped):
                loop.run()
            with mock.patch.object(harness_utils, "isOnline", lambda: True):
                self.assertIsNone(loop.run())
        self.assertIn("No headlines could be fetched", self.saidText(loop))


class ProgressTests(LoopTestCase):
    def testTheApprovedDraftIsRememberedAndOnlyGivenBackWhenResumed(self):
        loop = self.script(Loop(FakeAgent(["draft one", "draft two"])), ["yes", "yes"])
        saved = []
        loop.onProgress = lambda: saved.append(dict(loop.progress))
        self.assertEqual(loop.reviewLoop("task", lambda draft: "", key="mail"), "draft one")
        self.assertEqual((loop.progress, saved), ({"mail": "draft one"}, [{"mail": "draft one"}]))
        self.assertEqual(loop.reviewLoop("task", lambda draft: "", key="mail"), "draft two")
        loop.resumed = True
        self.assertEqual(loop.reviewLoop("task", lambda draft: "", key="mail"), "draft two")
        self.assertEqual(len(loop.agent.prompts), 2)

    def testNothingIsRememberedWithoutAKeyOrForARejectedDraft(self):
        loop = self.script(Loop(FakeAgent(["one"])), ["yes"])
        self.assertEqual(loop.reviewLoop("task", lambda draft: ""), "one")
        self.assertEqual(loop.progress, {})
        rejected = self.script(Loop(FakeAgent(["x"])), ["no"])
        self.assertIsNone(rejected.reviewLoop("task", lambda draft: "", key="mail"))
        self.assertEqual(rejected.progress, {})

    def testTheStateOfALoopSurvivesTheJsonAndIsACopy(self):
        loop = Loop(FakeAgent())
        loop.receive("Writer", "hello")
        loop.receiveFromUser("be short")
        loop.approvedPlan = "the plan"
        loop.remember("mail", "approved text")
        loop.logAction("Sent the email.")
        state = json.loads(json.dumps(loop.getState()))
        other = Loop(FakeAgent())
        other.setState(state)
        self.assertEqual((other.inbox, other.userMessages, other.approvedPlan, other.progress), (["Writer: hello"], ["be short"], "the plan", {"mail": "approved text"}))
        self.assertEqual([action["text"] for action in other.actions], ["Sent the email."])
        loop.progress["mail"] = "changed later"
        self.assertEqual(state["progress"], {"mail": "approved text"})
        other.setState({})
        self.assertEqual((other.inbox, other.progress, other.actions, other.approvedPlan), ([], {}, [], ""))

    def testEveryChangeOfTheProgressIsAnnounced(self):
        loop, calls = Loop(FakeAgent()), []
        loop.onProgress = lambda: calls.append(1)
        loop.remember("a", 1)
        loop.logAction("did it")
        self.assertEqual(len(calls), 2)

    def testAMessageIsSentOnceEvenAfterAResume(self):
        loop, sent = self.script(Loop(FakeAgent()), []), []
        self.assertTrue(loop.sendOnce("sent", lambda: sent.append(1), "sending"))
        self.assertEqual(loop.progress["sent"], "sent")
        loop.resumed = True
        self.assertFalse(loop.sendOnce("sent", lambda: sent.append(2), "sending"))
        self.assertEqual(sent, [1])

    def testWhenNobodyKnowsIfItLeftTheUserDecides(self):
        for answer, again in (("yes", True), ("no", False)):
            loop, sent, questions = Loop(FakeAgent()), [], []
            loop.askUser = lambda question: questions.append(question) or answer
            loop.resumed, loop.progress["sent"] = True, "sending"
            self.assertEqual(loop.sendOnce("sent", lambda: sent.append(1), "sending the email"), again)
            self.assertEqual(sent, [1] if again else [])
            self.assertEqual(loop.progress["sent"], "sent")
            self.assertEqual(len(questions), 1)
            self.assertIn("The program stopped while sending the email. It may or may not have gone out.", questions[0])

    def testAFailedSendIsForgottenSoItCanBeTriedAgain(self):
        loop = self.script(Loop(FakeAgent()), [])
        with self.assertRaises(ValueError):
            loop.sendOnce("sent", lambda: failing(ValueError("refused")), "sending")
        self.assertEqual(loop.progress["sent"], "")
        loop.resumed = True
        self.assertTrue(loop.sendOnce("sent", lambda: None, "sending"))

    def testAnApprovedEmailIsNotDraftedAgainAndIsSentOnceAfterAResume(self):
        smtp = mock.patch.object(harness_utils.smtplib, "SMTP")
        started = smtp.start()
        self.addCleanup(smtp.stop)
        loop = self.script(EmailLoop(FakeAgent(), "me@example.com", "sara@example.com", "Meeting", "Ask"), [])
        loop.settings.update(EMAIL_PASSWORD="pw", EMAIL_SMTP_SERVER="smtp.test")
        loop.setState({"progress": {"mail": "Approved before the stop"}})
        loop.resumed = True
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EMAIL_IMAP_SERVER", None)
            self.assertEqual(loop.run(), "Approved before the stop")
        self.assertEqual(loop.agent.prompts, [])
        sent = started.return_value.__enter__.return_value.send_message
        sent.assert_called_once()
        self.assertEqual(sent.call_args[0][0].get_content().strip(), "Approved before the stop")
        loop.resumed = True
        loop.run()
        sent.assert_called_once()


class FolderTests(LoopTestCase):
    settings = EmailTests.settings
    useMail = EmailTests.useMail
    fakeFeeds = NewsTests.fakeFeeds

    def setUp(self):
        super().setUp()
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        self.work = Path(work.name).resolve()

    def testAnAgentWithoutAFolderChangesNothing(self):
        loop = self.script(AuthorLoop(FakeAgent(["A text"]), "birds", 50), ["yes"])
        loop.setFolder(None)
        self.assertEqual((loop.folder, loop.describeFolder()), (None, ""))
        self.assertIsNone(loop.saveResult("text", "hello"))
        loop.run()
        self.assertEqual(list(self.work.iterdir()), [])
        self.assertEqual(loop.progress.get("stamp"), None)

    def testTheFolderMustExistAndCanBeChangedOrRemoved(self):
        loop = Loop(FakeAgent())
        with self.assertRaisesRegex(ValueError, "There is no folder at"):
            loop.setFolder(self.work / "nope")
        self.assertIsNone(loop.folder)
        loop.setFolder(self.work)
        self.assertEqual(loop.folder, self.work)
        self.assertIn(f"It works inside the folder {self.work}.", loop.describeFolder())
        with self.assertRaisesRegex(ValueError, "There is no folder at"):
            loop.setFolder(self.work / "still-nope")
        self.assertEqual(loop.folder, self.work)
        loop.setFolder("")
        self.assertIsNone(loop.folder)

    def testTheHomeSignIsUnderstood(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.work), "USERPROFILE": str(self.work)}):
            loop = Loop(FakeAgent())
            loop.setFolder("~")
        self.assertEqual(loop.folder, self.work)

    def testTheFilesOfTheAgentMustBeInsideItsFolder(self):
        inside, outside = self.work / "paper.tex", self.folder / "other.tex"
        inside.write_text("Theorem 1.")
        outside.write_text("Theorem 2.")
        for build in (lambda path: MathCheckLoop(FakeAgent(), path), lambda path: DocumentFormatLoop(FakeAgent(), path, "IEEE"),
                      lambda path: CoderLoop(FakeAgent(), "task", path)):
            loop = build(inside)
            loop.setFolder(self.work)
            self.assertEqual(loop.folder, self.work)
            loop = build(outside)
            with self.assertRaisesRegex(ValueError, f"other.tex is not inside {self.work}"):
                loop.setFolder(self.work)
            self.assertIsNone(loop.folder)

    def testALinkThatLeavesTheFolderDoesNotCount(self):
        secret, link = self.folder / "secret.tex", self.work / "link.tex"
        secret.write_text("Theorem 3.")
        try:
            link.symlink_to(secret)
        except OSError:
            self.skipTest("this computer does not allow links")
        with self.assertRaisesRegex(ValueError, "link.tex is not inside"):
            MathCheckLoop(FakeAgent(), link).setFolder(self.work)

    def testTheTextAndTheNewsAndTheSurveyAreSavedInTheFolder(self):
        loop = self.script(AuthorLoop(FakeAgent(["A text about birds"]), "birds", 50), ["yes"])
        loop.setFolder(self.work)
        loop.run()
        [copy] = list(self.work.glob("text_*.md"))
        self.assertEqual(copy.read_text(encoding="utf-8"), "A text about birds")
        self.assertIn(f"Saved {copy}.", [action["text"] for action in loop.actions])
        loop = self.script(NewsLoop(FakeAgent(["Briefing"]), outlets=["BBC World"]), ["yes"])
        loop.setFolder(self.work)
        with self.fakeFeeds():
            loop.run()
        [copy] = list(self.work.glob("news_briefing_*.md"))
        self.assertEqual(copy.read_text(encoding="utf-8"), "Briefing")
        paper = {"title": "Real", "summary": "x" * 500, "link": "https://a.test/1"}
        with mock.patch.dict(harness_utils.PAPER_SEARCHES, {"A": lambda subject: [paper]}, clear=True):
            loop = self.script(LiteratureSurveyLoop(FakeAgent(["See Real https://a.test/1."]), "graphs", 100, searches=("A",)), ["yes"])
            loop.setFolder(self.work)
            loop.run()
        [copy] = list(self.work.glob("literature_survey_*.md"))
        self.assertEqual(copy.read_text(encoding="utf-8"), "See Real https://a.test/1.")

    def testACopyOfEverySentEmailIsSavedInTheFolder(self):
        self.useMail()
        loop = self.script(EmailLoop(FakeAgent(["Hello Sara", "OK"]), "me@example.com", "sara@example.com", "Meeting", "Ask"), ["yes"])
        loop.setFolder(self.work)
        loop.run()
        [copy] = list(self.work.glob("sent_email_*.txt"))
        self.assertEqual(copy.read_text(encoding="utf-8"), "To: sara@example.com\nSubject: Meeting\n\nHello Sara")

    def testTheCalendarFileIsAlsoWrittenInTheFolderAndTheUserIsToldWhere(self):
        event = json.dumps({"date": "2099-05-04", "time": "10:00", "duration": 60, "subject": "Lunch"})
        loop = self.script(CalendarLoop(FakeAgent([event]), "Lunch"), ["yes"])
        loop.setFolder(self.work)
        loop.run()
        self.assertEqual((self.work / "calendar_events.ics").read_bytes(), (self.folder / "calendar_events.ics").read_bytes())
        self.assertIn(f"Booked. Import {self.work / 'calendar_events.ics'} into your calendar app.", loop.said)
        self.assertIn("Booked \"Lunch\" on 2099-05-04 at 10:00", loop.actions[-1]["text"])

    def testTheFormattedCopyIsNextToTheDocumentInsideTheFolder(self):
        document = self.work / "notes.md"
        document.write_text("Introduction\nSome words here.", encoding="utf-8")
        loop = self.script(DocumentFormatLoop(FakeAgent(["# Introduction\n\nSome words here."]), document, "markdown"), ["yes"])
        loop.setFolder(self.work)
        loop.run()
        self.assertTrue((self.work / "notes_formatted.md").exists())
        self.assertIn(f"Wrote the formatted copy {self.work / 'notes_formatted.md'}.", [action["text"] for action in loop.actions])

    def testTheCodeIsCheckedByACommandThatStartsInTheFolder(self):
        target = self.work / "solution.py"
        command = [sys.executable, "-c", "import os; open('where.txt', 'w').write(os.getcwd())"]
        loop = self.script(CoderLoop(FakeAgent(["print('ok')"]), "print ok", target, testCommand=command), ["yes", "yes"])
        loop.setFolder(self.work)
        loop.run()
        self.assertEqual((self.work / "where.txt").read_text(), str(self.work))

    def testAFileGivenWithARelativePathStillRunsWithTheDefaultCommand(self):
        before = os.getcwd()
        self.addCleanup(os.chdir, before)
        os.chdir(self.work.parent)
        relative = Path(self.work.name) / "solution.py"
        loop = self.script(CoderLoop(FakeAgent(["print('ok')"]), "print ok", relative), ["yes", "yes"])
        loop.setFolder(self.work)
        self.assertTrue(Path(loop.testCommand[1]).is_absolute())
        self.assertEqual(loop.run(), "print('ok')")
        self.assertEqual((self.work / "solution.py").read_text(encoding="utf-8"), "print('ok')")

    def testTheResultOfAResumedRunIsWrittenAgainInTheSameFile(self):
        first = self.script(AuthorLoop(FakeAgent(["Text"]), "birds", 50), ["yes"])
        first.setFolder(self.work)
        first.run()
        again = self.script(AuthorLoop(FakeAgent(), "birds", 50), [])
        again.setFolder(self.work)
        again.setState(first.getState())
        again.resumed = True
        again.run()
        self.assertEqual(len(list(self.work.glob("text_*.md"))), 1)


if __name__ == "__main__":
    unittest.main()
