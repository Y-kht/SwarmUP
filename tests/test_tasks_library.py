import json
import re
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

# The modules of SwarmUP are in the folders of src/backend. Their names have hyphens, so they are not packages: each folder goes on the path.
sys.path[:0] = [str(folder) for folder in sorted((Path(__file__).resolve().parent.parent / "src" / "backend").iterdir()) if folder.is_dir() and not folder.name.startswith(("_", "."))]
import harness_utils
from harness_utils import AGENT_RULES, AuthorLoop, CalendarLoop, CoderLoop, DocumentFormatLoop, EmailLoop, LiteratureSurveyLoop, MathCheckLoop, NewsLoop, loadRules
from models_library import RECOMMENDED_API, RECOMMENDED_LOCAL
from sources_library import EMAIL_PROVIDERS, MESSAGING_APPS
from tasks_library import (ADVANCED_FIELDS, NO_MESSENGER, OTHER_PROVIDER, TASKS, answerKey, buildLoop, checkAgentName, describeLoop, getDefault, getHelp, isAsked,
                           messengerSettings, parseAnswer, parseChoices, publicAnswers, restoreAnswers, secretFields, suggestFolder, suggestName)

KINDS = {"text", "email", "secret", "number", "file", "path", "folder", "phone", "time", "command", "choice", "choices", "outlets", "publishers", "accounts"}


class CatalogTests(unittest.TestCase):
    def testEveryTaskIsDescribedAndHasRecommendedModels(self):
        for key, task in TASKS.items():
            self.assertTrue(all(task[field] for field in ("label", "name", "role", "info", "fields")), key)
            self.assertIn(task["recommend"], RECOMMENDED_LOCAL, key)
            self.assertIn(task["recommend"], RECOMMENDED_API, key)
            self.assertEqual(checkAgentName(task["name"]), "", key)
            self.assertTrue(callable(task["build"]), key)

    def testEveryFieldHasAKnownKindAndAUniqueKey(self):
        for key, task in TASKS.items():
            keys = [field["key"] for field in task["fields"] + ADVANCED_FIELDS]
            self.assertEqual(len(keys), len(set(keys)), key)
            for field in task["fields"] + ADVANCED_FIELDS:
                self.assertIn(field["kind"], KINDS, (key, field["key"]))
                self.assertTrue(field["ask"].strip(), (key, field["key"]))
                if field["kind"] in ("choice", "choices"):
                    self.assertTrue(field["options"], (key, field["key"]))

    def testEveryTaskSaysWhatItsAgentDoesWithAFolder(self):
        for key, task in TASKS.items():
            self.assertTrue(task["folder"].strip().endswith("."), key)

    def testOnlyTheNewsAgentAsksAboutAMessagingApp(self):
        for key, task in TASKS.items():
            asked = [field["key"] for field in task["fields"] if field["key"] == "messenger"]
            self.assertEqual(bool(asked), key == "news", key)

    def testTheQuestionsOfAMessagingAppAreOnlyAskedWhenThatAppIsChosen(self):
        fields = {field["key"]: field for field in TASKS["news"]["fields"]}
        choice = fields["messenger"]
        self.assertEqual((choice["kind"], choice["default"], choice["options"]), ("choice", NO_MESSENGER, [NO_MESSENGER, "Telegram", "WhatsApp"]))
        for app, details in MESSAGING_APPS.items():
            for field in details["fields"]:
                follow = fields[answerKey(app, field["key"])]
                self.assertEqual((follow["ask"], follow["kind"]), (field["ask"], field["kind"]))
                self.assertTrue(isAsked(follow, {"messenger": app}))
                self.assertFalse(isAsked(follow, {"messenger": NO_MESSENGER}))
                self.assertFalse(isAsked(follow, {}))
                self.assertFalse(isAsked(follow, {"messenger": [other for other in MESSAGING_APPS if other != app][0]}))
        self.assertTrue(isAsked(choice, {}))
        self.assertEqual(sorted(key for key in fields if key.startswith(("telegram", "whatsapp"))),
                         ["telegramChat", "telegramToken", "whatsappPhoneId", "whatsappTo", "whatsappToken"])

    def testTheTasksOfTheDesignAreThere(self):
        self.assertEqual(set(TASKS), {"email", "calendar", "news", "author", "literature", "format", "math", "coder"})
        self.assertEqual(len({task["name"] for task in TASKS.values()}), len(TASKS))
        self.assertEqual(len({task["label"] for task in TASKS.values()}), len(TASKS))

    def testEmailNeedsCredentialsAndNewsNeedsOutletsAndATime(self):
        email = {field["key"]: field for field in TASKS["email"]["fields"]}
        self.assertEqual(email["password"]["kind"], "secret")
        self.assertTrue(all(email[key].get("required") for key in ("sender", "password", "smtp", "receiver", "subject", "request")))
        self.assertEqual(email["imap"].get("required"), None)
        news = {field["key"]: field for field in TASKS["news"]["fields"]}
        self.assertEqual((news["outlets"]["kind"], news["collectAt"]["kind"]), ("outlets", "time"))
        self.assertTrue(news["outlets"]["required"])
        literature = {field["key"]: field for field in TASKS["literature"]["fields"]}
        self.assertEqual((literature["publishers"]["kind"], literature["accounts"]["kind"]), ("publishers", "accounts"))
        self.assertEqual(literature["searches"]["default"], list(harness_utils.PAPER_SEARCHES))


class BuildTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.document = self.folder / "paper.tex"
        self.document.write_text("Theorem 1. A statement.")

    def answers(self):
        return {
            "email": {"provider": "Gmail", "sender": "me@gmail.com", "password": "app-password", "smtp": "smtp.gmail.com", "imap": "imap.gmail.com",
                      "receiver": "sara@example.com", "subject": "Meeting", "request": "move it", "language": "French"},
            "calendar": {"request": "dentist"},
            "news": {"outlets": ["BBC World", "https://example.com/feed"], "topics": "science", "collectAt": "07:30", "maxWords": 120, "language": "German"},
            "author": {"subject": "the sea", "length": 300},
            "literature": {"subject": "graphs", "length": 400, "searches": ["arXiv", "Crossref"], "publishers": {"IEEE": 263}, "accounts": {"ieeexplore.ieee.org": ("me", "account-secret-77")}},
            "format": {"filePath": str(self.document), "style": "IEEE"},
            "math": {"filePath": str(self.document)},
            "coder": {"task": "sort a list", "filePath": str(self.folder / "sort.py"), "testCommand": ["python", "-m", "pytest"]},
        }

    def testEveryTaskBuildsItsLoopWithTheModelAndDescribesItself(self):
        classes = {"email": EmailLoop, "calendar": CalendarLoop, "news": NewsLoop, "author": AuthorLoop, "literature": LiteratureSurveyLoop,
                   "format": DocumentFormatLoop, "math": MathCheckLoop, "coder": CoderLoop}
        for key, answers in self.answers().items():
            model = object()
            loop = TASKS[key]["build"](model, answers)
            self.assertIsInstance(loop, classes[key], key)
            self.assertIs(loop.agent, model, key)
            self.assertEqual(loop.numberOfLoops, 5, key)
            self.assertTrue(loop.describeTask(), key)
            self.assertEqual(TASKS[key]["build"](None, {**answers, "numberOfLoops": 2}).numberOfLoops, 2, key)

    def testTheEmailLoopHasItsCredentialsSoItNeverAsksForThem(self):
        loop = TASKS["email"]["build"](None, self.answers()["email"])
        self.assertEqual(loop.settings, {"EMAIL_PASSWORD": "app-password", "EMAIL_SMTP_SERVER": "smtp.gmail.com", "EMAIL_IMAP_SERVER": "imap.gmail.com"})
        self.assertEqual((loop.sender, loop.receiver, loop.subject, loop.language), ("me@gmail.com", "sara@example.com", "Meeting", "French"))
        loop = TASKS["email"]["build"](None, {**self.answers()["email"], "imap": ""})
        self.assertNotIn("EMAIL_IMAP_SERVER", loop.settings)

    def testTheNewsLoopHasItsOutletsAndItsTime(self):
        loop = TASKS["news"]["build"](None, self.answers()["news"])
        self.assertEqual((loop.outlets, loop.topics, loop.maxWords, loop.language), (("BBC World", "https://example.com/feed"), "science", 120, "German"))
        self.assertEqual((loop.startTime().hour, loop.startTime().minute), (7, 30))
        self.assertIsInstance(loop.startTime(), datetime)
        self.assertIsNone(TASKS["news"]["build"](None, {**self.answers()["news"], "collectAt": None}).startTime())

    def testTheNewsLoopSendsToTheMessagingAppThatWasChosen(self):
        news = self.answers()["news"]
        loop = TASKS["news"]["build"](None, news)
        self.assertEqual((loop.messenger, loop.messengerSettings), (None, {}))
        loop = TASKS["news"]["build"](None, {**news, "messenger": NO_MESSENGER, "telegramToken": "ignored"})
        self.assertEqual((loop.messenger, loop.messengerSettings), (None, {}))
        loop = TASKS["news"]["build"](None, {**news, "messenger": "Telegram", "telegramToken": "123:ABC", "telegramChat": "42", "whatsappToken": "ignored"})
        self.assertEqual((loop.messenger, loop.messengerSettings), ("Telegram", {"token": "123:ABC", "chat": "42"}))
        loop = TASKS["news"]["build"](None, {**news, "messenger": "WhatsApp", "whatsappToken": "t", "whatsappPhoneId": "555", "whatsappTo": "+4915112345678"})
        self.assertEqual((loop.messenger, loop.messengerSettings), ("WhatsApp", {"token": "t", "phoneId": "555", "to": "+4915112345678"}))
        self.assertEqual(messengerSettings({"messenger": "Telegram", "telegramToken": "t"}), {"token": "t", "chat": ""})
        self.assertEqual(messengerSettings({}), {})

    def testEveryAgentHasItsRulesAndEveryRulesFileBelongsToAnAgent(self):
        used = {TASKS[key]["build"](None, answers).rules for key, answers in self.answers().items()}
        self.assertNotIn("", used)
        self.assertEqual(len(used), len(TASKS))
        files = {file.name: loadRules(file.name) for file in AGENT_RULES.glob("*_RULES.md")}
        self.assertEqual(set(files.values()), used)
        self.assertTrue(all(re.fullmatch(r"[A-Z]+(_[A-Z]+)*_RULES\.md", name) for name in files), sorted(files))
        self.assertTrue(all(rules.startswith("# ") and "\n1) " in rules for rules in files.values()))

    def testALoopWorksInsideItsFolderAndRefusesAFolderThatCannotHoldItsFile(self):
        answers = {**self.answers()["format"], "folder": str(self.folder)}
        loop = buildLoop("format", None, answers)
        self.assertEqual(loop.folder, self.folder.resolve())
        self.assertIn(f"It works inside the folder {self.folder.resolve()}.", describeLoop(loop))
        self.assertEqual(describeLoop(buildLoop("format", None, self.answers()["format"])), TASKS["format"]["build"](None, self.answers()["format"]).describeTask())
        elsewhere = tempfile.TemporaryDirectory()
        self.addCleanup(elsewhere.cleanup)
        with self.assertRaisesRegex(ValueError, "paper.tex is not inside"):
            buildLoop("format", None, {**answers, "folder": elsewhere.name})
        with self.assertRaisesRegex(ValueError, "There is no folder at"):
            buildLoop("author", None, {**self.answers()["author"], "folder": str(self.folder / "missing")})
        self.assertIsNone(buildLoop("author", None, {**self.answers()["author"], "folder": None}).folder)

    def testTheFolderSuggestedIsTheOneOfTheFileOfTheAgent(self):
        self.assertEqual(suggestFolder(self.answers()["math"]), str(self.folder.resolve()))
        self.assertEqual(suggestFolder(self.answers()["coder"]), str(self.folder.resolve()))
        self.assertIsNone(suggestFolder(self.answers()["author"]))

    def testSecretsNeverGoInTheAnswersThatAreSavedAndComeBackWhenGiven(self):
        news = {**self.answers()["news"], "messenger": "Telegram", "telegramToken": "123:ABC", "telegramChat": "42"}
        saved = publicAnswers("news", news)
        self.assertEqual((saved["messenger"], saved["telegramChat"]), ("Telegram", "42"))
        self.assertNotIn("telegramToken", saved)
        email = publicAnswers("email", self.answers()["email"])
        self.assertNotIn("password", email)
        self.assertEqual(email["sender"], "me@gmail.com")
        literature = publicAnswers("literature", self.answers()["literature"])
        self.assertNotIn("accounts", literature)
        for key, answers in {**self.answers(), "news": news}.items():
            for secret in ("app-password", "123:ABC", "account-secret-77"):
                self.assertNotIn(secret, json.dumps(publicAnswers(key, answers)), key)
        restored = restoreAnswers("news", saved, {"telegramToken": "123:NEW"})
        self.assertEqual(restored["telegramToken"], "123:NEW")
        loop = TASKS["news"]["build"](None, restored)
        self.assertEqual(loop.messengerSettings, {"token": "123:NEW", "chat": "42"})
        empty = restoreAnswers("email", publicAnswers("email", self.answers()["email"]))
        self.assertEqual(empty["password"], "")
        self.assertEqual(restoreAnswers("literature", literature)["accounts"], {})
        self.assertEqual(TASKS["literature"]["build"](None, restoreAnswers("literature", literature)).logins, {})

    def testOnlyTheSecretsThatWereAskedAreAskedAgain(self):
        news = self.answers()["news"]
        self.assertEqual(secretFields("news", news), [])
        self.assertEqual([field["key"] for field in secretFields("news", {**news, "messenger": "Telegram"})], ["telegramToken"])
        self.assertEqual([field["key"] for field in secretFields("news", {**news, "messenger": "WhatsApp"})], ["whatsappToken"])
        self.assertEqual([field["key"] for field in secretFields("email", self.answers()["email"])], ["password"])
        self.assertEqual(secretFields("author", self.answers()["author"]), [])

    def testTheLiteratureLoopHasItsSearchesPublishersAndAccounts(self):
        loop = TASKS["literature"]["build"](None, self.answers()["literature"])
        self.assertEqual((loop.searches, loop.publishers, loop.logins), (("arXiv", "Crossref"), {"IEEE": 263}, {"ieeexplore.ieee.org": ("me", "account-secret-77")}))

    def testTheCoderLoopHasItsCommandOrRunsTheFileWithPython(self):
        loop = TASKS["coder"]["build"](None, self.answers()["coder"])
        self.assertEqual(loop.testCommand, ["python", "-m", "pytest"])
        loop = TASKS["coder"]["build"](None, {**self.answers()["coder"], "testCommand": None})
        self.assertEqual(loop.testCommand, [sys.executable, str(self.folder / "sort.py")])


class ParseAnswerTests(unittest.TestCase):
    def field(self, kind, **options):
        return {"key": "x", "ask": "Question?", "kind": kind, **options}

    def testRequiredFieldsNeedAnAnswerAndOptionalOnesGiveTheirDefault(self):
        self.assertEqual(parseAnswer(self.field("text", required=True), "  "), (None, "This answer is needed."))
        self.assertEqual(parseAnswer(self.field("text", required=True), "  hello  "), ("hello", ""))
        self.assertEqual(parseAnswer(self.field("text"), ""), ("", ""))
        self.assertEqual(parseAnswer(self.field("text", default="English"), ""), ("English", ""))
        self.assertEqual(parseAnswer(self.field("number", default=5), ""), (5, ""))
        self.assertEqual(parseAnswer(self.field("time", default=None), ""), (None, ""))
        self.assertEqual(parseAnswer(self.field("command", default=None), ""), (None, ""))

    def testTheDefaultCanDependOnWhatWasAnsweredBefore(self):
        field = self.field("text", required=True, default=lambda answers: {"Gmail": "smtp.gmail.com"}.get(answers["provider"]))
        self.assertEqual(parseAnswer(field, "", {"provider": "Gmail"}), ("smtp.gmail.com", ""))
        self.assertEqual(parseAnswer(field, "", {"provider": "Other"}), (None, "This answer is needed."))
        self.assertEqual(parseAnswer(field, "mine.test", {"provider": "Gmail"}), ("mine.test", ""))
        self.assertEqual(getDefault(field, {"provider": "Gmail"}), "smtp.gmail.com")
        self.assertEqual(getHelp(self.field("text", help=lambda answers: f"for {answers['provider']}"), {"provider": "Gmail"}), "for Gmail")
        self.assertEqual(getHelp(self.field("text"), {}), "")

    def testEmailAddressesAreChecked(self):
        self.assertEqual(parseAnswer(self.field("email"), "sara@example.com"), ("sara@example.com", ""))
        for bad in ("sara", "sara@", "@example.com", "sara@example", "sara example@x.com"):
            self.assertIn("not an email address", parseAnswer(self.field("email"), bad)[1], bad)

    def testSecretsAreKeptExactlyAsTyped(self):
        self.assertEqual(parseAnswer(self.field("secret", required=True), "  p a s s  "), ("  p a s s  ", ""))
        self.assertEqual(parseAnswer(self.field("secret", required=True), "")[1], "This answer is needed.")

    def testNumbersMustBeWholeAndAboveZero(self):
        self.assertEqual(parseAnswer(self.field("number"), " 12 "), (12, ""))
        for bad in ("0", "-3", "2.5", "many", "1e3"):
            self.assertEqual(parseAnswer(self.field("number"), bad), (None, "Write a whole number above 0."), bad)

    def testFilesAndPathsAreChecked(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        existing = Path(folder.name) / "a.txt"
        existing.write_text("x")
        self.assertEqual(parseAnswer(self.field("file"), str(existing)), (str(existing), ""))
        self.assertIn("There is no file at", parseAnswer(self.field("file"), str(Path(folder.name) / "missing.txt"))[1])
        self.assertIn("There is no file at", parseAnswer(self.field("file"), folder.name)[1])
        new = Path(folder.name) / "new.py"
        self.assertEqual(parseAnswer(self.field("path"), str(new)), (str(new), ""))
        self.assertEqual(parseAnswer(self.field("path"), str(existing)), (str(existing), ""))
        self.assertIn("is a folder", parseAnswer(self.field("path"), folder.name)[1])
        self.assertIn("does not exist", parseAnswer(self.field("path"), str(Path(folder.name) / "nowhere" / "new.py"))[1])

    def testFoldersMustExistAndAreGivenInFull(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        existing = Path(folder.name) / "a.txt"
        existing.write_text("x")
        self.assertEqual(parseAnswer(self.field("folder"), f"  {folder.name} "), (str(Path(folder.name).resolve()), ""))
        self.assertIn("There is no folder at", parseAnswer(self.field("folder"), str(Path(folder.name) / "missing"))[1])
        self.assertIn("There is no folder at", parseAnswer(self.field("folder"), str(existing))[1])
        self.assertEqual(parseAnswer(self.field("folder"), ""), (None, ""))

    def testPhoneNumbersNeedTheirCountryCode(self):
        for text, number in (("+4915112345678", "+4915112345678"), (" +49 151 1234 5678 ", "+4915112345678"), ("0049 151 12345678", "+4915112345678"),
                             ("(+1) 650-555-1234", "+16505551234"), ("+1.650.555.1234", "+16505551234")):
            self.assertEqual(parseAnswer(self.field("phone"), text), (number, ""), text)
        for bad in ("0151 12345678", "+12", "+49 abc", "hello", "+1234567890123456"):
            self.assertEqual(parseAnswer(self.field("phone"), bad), (None, "Write the number with its country code, like +4915112345678."), bad)

    def testTimesAreCheckedAndWrittenWithTwoDigits(self):
        for text, value in (("07:30", "07:30"), (" 7:05 ", "07:05"), ("23:59", "23:59"), ("0:0", "00:00")):
            self.assertEqual(parseAnswer(self.field("time"), text), (value, ""), text)
        for bad in ("7h30", "24:00", "12:60", "noon", "12:30:10", "12"):
            self.assertIn("HH:MM", parseAnswer(self.field("time"), bad)[1], bad)

    def testCommandsAreSplitLikeAShellDoes(self):
        self.assertEqual(parseAnswer(self.field("command"), 'python -m pytest "my tests" -q'), (["python", "-m", "pytest", "my tests", "-q"], ""))
        self.assertIn("cannot be read", parseAnswer(self.field("command"), 'python "unclosed')[1])


class ChoiceTests(unittest.TestCase):
    options = ["Gmail", "Yahoo", "Zoho", "iCloud"]

    def testNumbersRangesAndLabelsChooseOptions(self):
        self.assertEqual(parseChoices(self.options, "2"), (["Yahoo"], ""))
        self.assertEqual(parseChoices(self.options, "1, 3-4"), (["Gmail", "Zoho", "iCloud"], ""))
        self.assertEqual(parseChoices(self.options, "zoho,GMAIL"), (["Zoho", "Gmail"], ""))
        self.assertEqual(parseChoices(self.options, "2,2,1-2"), (["Yahoo", "Gmail"], ""))
        self.assertEqual(parseChoices(self.options, "3-3"), (["Zoho"], ""))

    def testMistakesAreExplained(self):
        self.assertEqual(parseChoices(self.options, "5"), ([], "Choose numbers between 1 and 4."))
        self.assertEqual(parseChoices(self.options, "0"), ([], "Choose numbers between 1 and 4."))
        self.assertEqual(parseChoices(self.options, "3-9"), ([], "Choose numbers between 1 and 4."))
        self.assertEqual(parseChoices(self.options, "4-2"), ([], "Choose numbers between 1 and 4."))
        self.assertEqual(parseChoices(self.options, "Hotmail"), ([], "'Hotmail' is not one of the choices."))
        self.assertEqual(parseChoices(self.options, " , "), ([], "Choose at least one."))
        self.assertEqual(parseChoices(self.options, ""), ([], "Choose at least one."))
        self.assertEqual(parseChoices(self.options, "1,2", one=True), ([], "Choose only one."))
        self.assertEqual(parseChoices(self.options, "1", one=True), (["Gmail"], ""))

    def testOptionsThatContainDashesStillWork(self):
        options = ["Al-Monitor", "Daily Mail"]
        self.assertEqual(parseChoices(options, "Al-Monitor"), (["Al-Monitor"], ""))
        self.assertEqual(parseChoices(options, "1-2"), (options, ""))


class NamesTests(unittest.TestCase):
    def testNamesAreCheckedAndSuggestedWithoutClashes(self):
        self.assertEqual(checkAgentName("Writer-2.a_b", ["Other"]), "")
        for bad in ("", "2Writer", "my writer", "Wri\"ter", "-x", "User", "user"):
            self.assertTrue(checkAgentName(bad), bad)
        self.assertIn("already an agent called writer", checkAgentName("writer", ["Writer"]))
        self.assertEqual(suggestName("author"), "Writer")
        self.assertEqual(suggestName("author", ["Writer"]), "Writer2")
        self.assertEqual(suggestName("author", ["writer", "Writer2", "Other"]), "Writer3")
        self.assertEqual(suggestName("math", ["Writer"]), "MathChecker")

    def testTheEmailProvidersHaveTheirServersAndTheirAdvice(self):
        self.assertIn("Gmail", EMAIL_PROVIDERS)
        for provider, (smtp, imap, advice) in EMAIL_PROVIDERS.items():
            self.assertTrue(smtp.count(".") >= 2 and imap.count(".") >= 2 and len(advice) > 20, provider)
        provider = {field["key"]: field for field in TASKS["email"]["fields"]}["provider"]
        self.assertEqual(provider["options"], [*EMAIL_PROVIDERS, OTHER_PROVIDER])


if __name__ == "__main__":
    unittest.main()
