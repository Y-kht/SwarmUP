import json
import re
import sys
import tempfile
import threading
import time
import unittest
from patching import everywhere
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# The modules of SwarmUP are in the folders of src/backend. Their names have hyphens, so they are not packages: each folder goes on the path.
sys.path[:0] = [str(folder) for folder in sorted((Path(__file__).resolve().parent.parent / "src" / "backend").iterdir()) if folder.is_dir() and not folder.name.startswith(("_", "."))]
import agent_prompts as prompts
import agent_storehouse
import harness_utils
import leader_catalog
import leader_checks
import leader_parser
import leader_utils
import mission_costs
from leader_catalog import LeaderCatalog
from leader_manager import LeaderManager
from leader_parser import findSuggestions, isNoChange, parseOutput
from leader_utils import designSwarm
from models_library import getModelInfo
from swarm_harness import Swarm
from test_live_swarm import GatedLoop
from test_saved_swarms import waitUntil
from writing_loops import LeaderLoop

GPUS = [{"name": "RTX A5000", "total": 24.0, "free": 23.5}]
# The prices of the tests, in US dollars per 1 million tokens, so that no test asks the internet for them.
PRICES = {"claude-opus-5-5": (5.0, 25.0, 0.5, 6.25), "claude-sonnet-5-5": (3.0, 15.0, 0.3, 3.75), "claude-haiku-4-5": (1.0, 5.0, 0.1, 1.25),
          "gpt-6-luna": (1.25, 10.0, 0.125, None), "gpt-4.1-mini": (0.4, 1.6, 0.1, None), "deepseek-v4-pro": (0.5, 2.0, 0.05, None)}


def fixedPrices():
    return {name: {"input": reading, "output": writing, "cachedInput": cached, "context": 200000, "cacheWrite": written, "inputLong": None, "outputLong": None,
                   "cachedInputLong": None, "cacheWriteLong": None} for name, (reading, writing, cached, written) in PRICES.items()}


# Every test that shows or checks a cost uses the prices above instead of the list of the internet.
def usePrices(case):
    patcher = everywhere(harness_utils, "loadModelPrices", fixedPrices)
    patcher.start()
    case.addCleanup(patcher.stop)
NAMES = ["Leader", "Writer", "Checker", "Researcher"]


# What the computer has, as a test wants it: GPUs, Codex signed in or not, and the packages of every kind of model.
def machine(gpus=GPUS, codex=False, claudeMissing=(), localMissing=(), apiMissing=None):
    return {"codex": {"ready": codex, "problem": "" if codex else "Codex is not installed.", "models": ["gpt-6-luna"] if codex else []}, "claudeMissing": list(claudeMissing),
            "localMissing": list(localMissing), "gpus": gpus, "apiMissing": {provider: [] for provider in ("gpt", "claude", "gemini", "deepseek")} | (apiMissing or {})}


def makeCatalog(folder, status=None, keys=None, leaderModel="claude-opus-5-5"):
    catalog = LeaderCatalog("Check my paper and write a summary of it.", folder, "Leader", getModelInfo(leaderModel) if leaderModel else None, keys={"claude": "sk"} if keys is None else keys)
    catalog.status, catalog.statusTime = status or machine(), time.monotonic() + 1e9
    return catalog


def block(action, **data):
    return f"<swarmup_{action}>\n{json.dumps(data)}\n</swarmup_{action}>"


# The model of a leader: what it answers to each kind of prompt of leader_utils.py comes from replies ({kind: [answers]}), in order.
# Without a reply left it has nothing to change, and it writes plans, summaries and reports like any model.
class LeaderModel:
    KINDS = (("SwarmUP could not use what you wrote", "repair"), ("wants changes:", "revise"), ("looks like a change of the swarm", "confirm"),
             ("Decide if the swarm needs a change now", "supervise"), ("Build the swarm for this mission", "build"))

    def __init__(self, **replies):
        self.replies = {kind: list(answers) for kind, answers in replies.items()}
        self.prompts = []
        self.usage = {"calls": 0, "input": 0, "output": 0}

    def input(self, prompt):
        self.prompts.append(prompt)
        for marker, kind in self.KINDS:
            if marker in prompt:
                return self.replies[kind].pop(0) if self.replies.get(kind) else "NO CHANGE"
        # The memory of the mission comes before the request, and the fake only reads the request.
        prompt = prompt.split(prompts.NOTES_PROMPT)[-1]
        names = re.findall(r"^- ([A-Za-z][\w.-]*) \(", prompt, re.M)
        if "final report" in prompt:
            return "Report: every agent did its work."
        if "summary" in prompt.lower() and names:
            return "Where the team stands: " + ", ".join(names) + "."
        if "write a plan" in prompt.lower():
            return "1. Follow the agents. 2. Write the report."
        return "OK"

    def kinds(self):
        return [next((kind for marker, kind in self.KINDS if marker in prompt), "other") for prompt in self.prompts]


# The leader of a test: its proposals and its questions are answered by the test (decisions and answers, in order).
def makeLeader(model, decisions=(), answers=()):
    leader = LeaderLoop(model, "Check my paper and write a summary of it.")
    leader.proposals, leader.questions, leader.notes = [], [], []
    decisions, answers = list(decisions), list(answers)
    def propose(proposal):
        leader.proposals.append(proposal)
        return decisions.pop(0) if decisions else {"decision": "approve", "message": ""}
    def ask(questions):
        leader.questions.append(questions)
        return answers.pop(0) if answers else {}
    leader.askProposal, leader.askQuestions = propose, ask
    leader.notifyUser = leader.notes.append
    leader.askUser = lambda question: "yes"
    return leader


class FolderTestCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        (self.folder / "paper.tex").write_text("\\begin{theorem}A\\end{theorem}", encoding="utf-8")
        (self.folder / "notes").mkdir()
        (self.folder / "notes" / "ideas.md").write_text("ideas", encoding="utf-8")
        (self.folder / ".secret").write_text("hidden", encoding="utf-8")
        for target, name, value in ((harness_utils, "AGENT_FILES", self.folder / "agent-files"), (leader_utils, "DEBOUNCE_SECONDS", 0.02)):
            patcher = everywhere(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        usePrices(self)


class ParserTests(unittest.TestCase):
    def read(self, text):
        found = parseOutput(text)
        return [(item["action"], item["data"]) for item in found["blocks"]], found

    def testTheBlocksAsTheLeaderIsAskedToWriteThemAreRead(self):
        text = "Two changes.\n" + block("remove", name="Checker", why="Done.") + "\n" + block("add", name="Tr", task="author", model="claude-haiku-4-5", why="French.")
        blocks, found = self.read(text)
        self.assertEqual(blocks, [("remove", {"name": "Checker", "why": "Done."}), ("add", {"name": "Tr", "task": "author", "model": "claude-haiku-4-5", "why": "French."})])
        self.assertEqual((found["text"], found["problems"]), ("Two changes.", []))

    def testOtherSpellingsOfTheTagsAreRead(self):
        remove = {"name": "Checker"}
        for text in ('<SwarmUP-Remove>{"name": "Checker"}</SwarmUP-Remove>', '<swarmup remove>{"name": "Checker"}</swarmup remove>',
                     '[swarmup_remove]{"name": "Checker"}[/swarmup_remove]', '[[remove_agent]] {"name": "Checker"} [[/remove_agent]]',
                     '<<swarmup:remove>>{"name": "Checker"}<</swarmup:remove>>', '<swarmup_remove>{"name": "Checker"}<end_swarmup_remove>',
                     '<swarmup_remove>{"name": "Checker"}</swarmup>', '<delete_agent>{"name": "Checker"}</delete_agent>'):
            self.assertEqual(self.read(text)[0], [("remove", remove)], text)

    def testABlockWithoutItsClosingTagEndsWhereItsContentEnds(self):
        blocks, found = self.read('<swarmup_remove>\n{"name": "Checker", "why": "done"}\n\nThat frees the GPU.')
        self.assertEqual((blocks, found["text"]), ([("remove", {"name": "Checker", "why": "done"})], "That frees the GPU."))
        blocks, found = self.read("<swarmup_remove>\nname: Checker\nwhy: its work is done\n\nMore words.")
        self.assertEqual((blocks, found["text"]), ([("remove", {"name": "Checker", "why": "its work is done"})], "More words."))

    def testJsonInACodeFenceOrAloneInTheTextIsReadWhenItSaysWhichChangeItIs(self):
        cases = {'```swarmup_add\n{"name": "Tr", "task": "author", "model": "m"}\n```': [("add", {"name": "Tr", "task": "author", "model": "m"})],
                 'I propose:\n```json\n{"action": "remove_agent", "agent": "Checker", "reason": "done"}\n```': [("remove", {"agent": "Checker", "reason": "done"})],
                 'Here {"action": "change_model", "name": "Writer", "model": "claude-opus-5-5"} is it.': [("model", {"name": "Writer", "model": "claude-opus-5-5"})],
                 '```json\n[{"name": "A", "task": "author", "model": "m"}]\n```': [("build", {"agents": [{"name": "A", "task": "author", "model": "m"}]})],
                 '{"agents": {"A": {"task": "author", "model": "m"}}}': [("build", {"agents": [{"name": "A", "task": "author", "model": "m"}]})]}
        for text, expected in cases.items():
            self.assertEqual(self.read(text)[0], expected, text)

    def testJsonAsModelsWriteItIsRepaired(self):
        cases = ['<swarmup_add>\n{\n  // the translator\n  name: "Tr",\n  "task": \'author\',\n  "model": "m", # cheap\n  "settings": {"length": 300,},\n}\n</swarmup_add>',
                 "<swarmup_add>{'name': 'Tr', 'task': 'author', 'model': 'm', 'settings': {'length': 300}, 'local': False}</swarmup_add>",
                 '<swarmup_add>{“name”: “Tr”, “task”: “author”, “model”: “m”, "settings": {"length": 300}}</swarmup_add>',
                 '<swarmup_add>"name": "Tr", "task": "author", "model": "m", "settings": {"length": 300}</swarmup_add>']
        for text in cases:
            [(action, data)] = self.read(text)[0]
            self.assertEqual((action, data["name"], data["task"], data["settings"]), ("add", "Tr", "author", {"length": 300}), text)
        [(action, data)] = self.read('<swarmup_add>{"name": "N", "task": "news", "model": "m", "settings": {"outlets": ["https://example.org/feed"]}}</swarmup_add>')[0]
        self.assertEqual(data["settings"]["outlets"], ["https://example.org/feed"])

    def testSeveralChangesInOneBlockAreSeveralChanges(self):
        self.assertEqual(self.read('<swarmup_remove>{"name": ["A", "B"], "why": "done"}</swarmup_remove>')[0],
                         [("remove", {"name": "A", "why": "done"}), ("remove", {"name": "B", "why": "done"})])
        self.assertEqual([action for action, data in self.read('<swarmup_add>{"agents": [{"name": "A", "task": "author"}, {"name": "B", "task": "math"}]}</swarmup_add>')[0]], ["add", "add"])

    def testABrokenBlockOfSwarmUpGoesBackToTheLeader(self):
        blocks, found = self.read('Here: <swarmup_add>{"name": "Tr", "task": "author", </swarmup_add> done.')
        self.assertEqual(blocks, [])
        self.assertEqual(found["problems"][0]["action"], "add")
        self.assertIn("cannot be read as JSON", found["problems"][0]["error"])
        self.assertEqual(found["text"], "Here: done.")
        found = parseOutput('<swarmup_model>{"name": "Writer"}</swarmup_model>')
        self.assertIn('"name", "model" and "why"', found["problems"][0]["error"])

    def testWordsThatOnlyLookLikeBlocksStayInTheText(self):
        for text in ("The <model> field of an agent matters.", "See [add] in the menu, and [1] for the source.", '{"Writer": [], "Checker": ["Writer"]}',
                     '{"Writer": "Remove the introduction"}', "Visit https://example.org/{id} for details.", "A <b>bold</b> word and <remove this> text.",
                     '```json\n{"title": "A paper", "year": 2026}\n```', '<add>{"colour": "red"}</add>'):
            blocks, found = self.read(text)
            self.assertEqual((blocks, found["problems"], found["text"]), ([], [], text.strip()), text)

    def testNoChangeIsUnderstoodInItsUsualForms(self):
        for text in ("NO CHANGE", "No change.", "no changes needed", "**NO CHANGE**", " No-change"):
            self.assertTrue(isNoChange(text), text)
        for text in ("Changes are needed.", "I see no reason to stop. NO CHANGE", ""):
            self.assertFalse(isNoChange(text), text)


class SuggestionTests(unittest.TestCase):
    def testSentencesThatProposeAChangeAreFound(self):
        cases = {"I suggest removing Checker, since its work is done.": ("remove", "Checker"), "Checker is no longer needed.": ("remove", "Checker"),
                 "We should remove the agent Checker to free the GPU.": ("remove", "Checker"), "Researcher can now be removed.": ("remove", "Researcher"),
                 "It would be better to drop Researcher now.": ("remove", "Researcher"), "I removed Checker.": ("remove", "Checker"),
                 "I recommend adding a translator agent for the French version.": ("add", None), "Let's add another writer to speed things up.": ("add", None),
                 "The swarm would benefit from a fact-checker agent.": ("add", None), "I propose to switch Writer to claude-opus-5-5.": ("model", "Writer"),
                 "We could use a bigger model for Writer.": ("model", "Writer")}
        for sentence, (action, agent) in cases.items():
            found = findSuggestions(f"Here is the summary of the work. {sentence} Everything else is fine.", NAMES, "Leader")
            self.assertEqual([(item["action"], item["agent"], item["sentence"]) for item in found], [(action, agent, sentence)], sentence)

    def testSentencesThatOnlyTalkAboutTheAgentsAreNotChanges(self):
        for text in ("Checker is done and its result was sent to Writer.", "Checker left the swarm. Why: not needed.", "We should not remove Checker yet.",
                     "There is no need to remove Researcher.", "Writer will add the references to the article.", "Remove the second paragraph of Writer's text.",
                     "Writer removed the duplicate sentences.", "Checker was removed by the user.", "I suggest that Writer shortens the introduction.",
                     "We should add a conclusion to the text.", "Writer uses claude-sonnet-5-5 and is working.", "I don't think we need another agent.",
                     "Keep Checker until Writer is done, rather than removing it.", '{"Writer": "Remove the intro, we should drop Checker"}', "NO CHANGE",
                     "I suggest removing Leader.", "We could ask the user for the email address."):
            self.assertEqual(findSuggestions(text, NAMES, "Leader"), [], text)

    def testOnlyAFewSentencesAreSentBackAtOnce(self):
        text = " ".join(f"We should add a {noun} agent." for noun in ("writer", "coder", "tester", "editor", "planner"))
        self.assertEqual(len(findSuggestions(text, NAMES, "Leader")), leader_parser.MAX_SUGGESTIONS)


class CatalogTests(FolderTestCase):
    def check(self, catalog, raw, names=("Leader",), waitable=(), taken=0.0):
        return catalog.checkAgent(raw, list(names), list(waitable), taken)

    def testTasksAreFoundByTheirKeyLabelRoleOrUsualWords(self):
        catalog = makeCatalog(self.folder)
        for word, key in (("author", "author"), ("Writer (texts, essays, articles)", "author"), ("writer", "author"), ("Math checker", "math"), ("referee", "math"),
                          ("literature_reviewer", "literature"), ("Email writer and sender", "email"), ("programmer", "coder"), ("news-briefer", "news")):
            self.assertEqual(catalog.findTask(word), key, word)
        self.assertIsNone(catalog.findTask("poet"))
        self.assertIsNone(catalog.findTask("leader"))

    def testModelsAreFoundInTheWaysALeaderWritesThem(self):
        catalog = makeCatalog(self.folder, machine(codex=True))
        cases = {"claude-sonnet-5-5": ("claude-sonnet-5-5", None, None), "Claude Sonnet 5.5": ("claude-sonnet-5-5", None, None),
                 "anthropic/claude-sonnet-5-5": ("claude-sonnet-5-5", None, None), "qwen3-8b": ("Qwen/Qwen3-8B", None, 16), "Qwen/Qwen3-14B@4bit": ("Qwen/Qwen3-14B", None, 4),
                 "Qwen/Qwen3-14B (8-bit)": ("Qwen/Qwen3-14B", None, 8), "claude-code": ("default", "claude-code", None),
                 "Claude Code: claude-haiku-4-5": ("claude-haiku-4-5", "claude-code", None), "codex": ("default", "codex", None), "codex (gpt-6-luna)": ("gpt-6-luna", "codex", None)}
        for text, (name, cli, bits) in cases.items():
            info, problem = catalog.findModel(text)
            self.assertEqual((problem, info["name"], info["cli"], info["bits"]), ("", name, cli, bits), text)
        self.assertEqual(catalog.findModel({"name": "Qwen/Qwen3-8B", "bits": 4})[0]["vram"], 4.9)
        for text in ("gpt-9", "", "codex:gpt-9", "claude-code:gpt-6-luna", "someone/some-model"):
            self.assertIsNone(catalog.findModel(text)[0], text)

    def testAModelThatCannotRunHereIsRefusedWithTheReason(self):
        cases = [(machine(), "Qwen/Qwen3-32B", "only 24.0 GB are left"), (machine(gpus=[]), "Qwen/Qwen3-8B", "no supported GPU"),
                 (machine(localMissing=["torch"]), "Qwen/Qwen3-8B", "torch"), (machine(), "codex", "Codex cannot be used now: Codex is not installed."),
                 (machine(claudeMissing=["claude-agent-sdk"]), "claude-code", "claude-agent-sdk"), (machine(apiMissing={"gpt": ["openai"]}), "gpt-6-luna", "openai")]
        for status, model, words in cases:
            agent = self.check(makeCatalog(self.folder, status), {"name": "Writer", "task": "author", "model": model, "settings": {"subject": "bees"}})
            self.assertTrue(any(words in error for error in agent["errors"]), (model, agent["errors"]))
        agent = self.check(makeCatalog(self.folder), {"name": "Writer", "task": "author", "model": "Qwen/Qwen3-8B", "settings": {"subject": "bees"}}, taken=10.0)
        self.assertIn("the models already chosen take 10.0 GB", agent["errors"][0])

    def testTheSettingsAreFoundAndCheckedLikeTheFormsOfTheUser(self):
        catalog = makeCatalog(self.folder)
        agent = self.check(catalog, {"name": "fact checker", "task": "math", "model": "claude-sonnet-5-5", "file": "paper.tex", "provider": "anthropic", "why": "It checks."})
        self.assertEqual((agent["errors"], agent["name"], agent["values"]), ([], "FactChecker", {"filePath": str(self.folder / "paper.tex")}))
        self.assertTrue(agent["description"].startswith("Referee the mathematical text paper.tex"), agent["description"])
        self.assertNotIn("It works inside the folder", agent["description"])
        agent = self.check(catalog, {"name": "Writer", "task": "author", "model": "claude-sonnet-5-5", "settings": {"topic": "bees", "words": 250.0, "drafts": 3}})
        self.assertEqual((agent["errors"], agent["answers"]["subject"], agent["answers"]["length"], agent["answers"]["numberOfLoops"]), ([], "bees", 250, 3))
        agent = self.check(catalog, {"name": "News", "task": "news", "model": "claude-sonnet-5-5", "settings": {"outlets": "BBC World, https://example.org/rss", "time": "7:05"}})
        self.assertEqual((agent["errors"], agent["answers"]["outlets"], agent["answers"]["collectAt"]), ([], ["BBC World", "https://example.org/rss"], "07:05"))
        agent = self.check(catalog, {"name": "Survey", "task": "literature", "model": "claude-sonnet-5-5", "settings": {"subject": "bees", "searches": ["arxiv"], "publishers": ["ieee", "Elsevier"]}})
        self.assertEqual((agent["errors"], agent["answers"]["searches"], agent["answers"]["publishers"]), ([], ["arXiv"], {"IEEE": "263", "Elsevier": "78"}))
        agent = self.check(catalog, {"name": "Writer", "task": "author", "model": "claude-sonnet-5-5", "settings": {"subject": "bees", "length": "many", "colour": "red"}})
        self.assertEqual(agent["errors"], ["colour is not a setting of the task author. Its settings are: subject, length, numberOfLoops.", "length: Write a whole number above 0."])
        agent = self.check(catalog, {"name": "Checker", "task": "math", "model": "claude-sonnet-5-5", "settings": {"filePath": "missing.tex"}})
        self.assertEqual(agent["errors"], [f"filePath: There is no file at {self.folder / 'missing.tex'}."])
        agent = self.check(catalog, {"name": "Mail", "task": "email", "model": "claude-sonnet-5-5", "settings": {"provider": "Outlook", "to": "not an address", "subject": "s", "request": "r"}})
        self.assertEqual(agent["errors"], ["receiver: This is not an email address. It looks like name@example.com."])
        self.assertEqual(agent["answers"]["provider"], "Outlook / Hotmail / Microsoft 365")

    def testPasswordsAreNeverTakenFromTheLeaderAndTheUserIsAskedForWhatTheLeaderCannotKnow(self):
        catalog = makeCatalog(self.folder, keys={})
        agent = self.check(catalog, {"name": "Mailer", "task": "email", "model": "gpt-6-luna",
                                     "settings": {"provider": "Gmail", "to": "prof@uni.edu", "subject": "Summary", "request": "Send the summary", "password": "hunter2"}})
        self.assertEqual(agent["errors"], [])
        self.assertNotIn("password", agent["values"])
        self.assertEqual(([field["key"] for field in agent["needs"]], agent["key"]), (["sender", "password"], "gpt"))
        shown = leader_checks.describeAgent(agent)
        self.assertNotIn("hunter2", json.dumps(shown))
        self.assertEqual(shown["needs"], ["Your email address (the one the email is sent from)", "Password of that account", "An API key of OpenAI"])

    def testTheLeaderGivesInstructionsAndRulesToGeneralAgents(self):
        catalog = makeCatalog(self.folder, keys={"claude": "sk"})
        agents, errors = catalog.checkBuild({"agents": [
            {"name": "Analyst", "model": "claude-sonnet-5-5", "settings": {"instructions": "List the findings of the notes", "rules": "AUTHOR_RULES.md, coder"}},
            {"name": "Drafter", "task": "general", "model": "claude-sonnet-5-5", "settings": {"prompt": "Write the email"}}]})
        self.assertEqual(errors, [])
        self.assertEqual([(agent["task"], agent["answers"]["prompt"], agent["answers"]["rules"]) for agent in agents],
                         [("agent", "List the findings of the notes", ["Author", "Coder"]), ("agent", "Write the email", [])])
        agents, errors = catalog.checkBuild({"agents": [{"name": "A", "task": "agent", "model": "claude-sonnet-5-5", "settings": {"prompt": "x", "rules": ["Cooking"]}}]})
        self.assertTrue(any("Cooking" in error for error in errors), errors)
        tasks = leader_catalog.describeTasks()
        self.assertLess(tasks.index("THE GENERAL AGENT"), tasks.index("THE SPECIALISED TASKS"))
        self.assertIn("    Author: rules for the text the agent writes.", tasks)
        self.assertIn('"task": "agent"', catalog.buildPrompt())

    def testTheWholeSwarmIsCheckedTogether(self):
        catalog = makeCatalog(self.folder, leaderModel="Qwen/Qwen3-8B")
        agents, errors = catalog.checkBuild({"agents": [{"name": "A", "task": "author", "model": "Qwen/Qwen3-8B", "settings": {"subject": "a"}},
                                                        {"name": "B", "task": "author", "model": "claude-sonnet-5-5", "waits_for": ["A"], "settings": {"subject": "b"}}]})
        self.assertEqual(errors, ["Agent 1 (A): Qwen/Qwen3-8B needs 19.7 GB of VRAM at 16 bits, but only 4.3 GB are left of the 24.0 GB of the GPUs (the models already chosen take 19.7 GB). "
                                  "Choose a smaller model, fewer bits, or an API model."])
        agents, errors = catalog.checkBuild({"agents": [{"name": "A", "task": "author", "model": "Qwen/Qwen3.5-0.8B", "waits_for": "B", "settings": {"subject": "a"}},
                                                        {"name": "B", "task": "author", "model": "claude-sonnet-5-5", "waits_for": ["A"], "settings": {"subject": "b"}}]})
        self.assertEqual(len(errors), 1)
        self.assertIn("wait for each other in a circle", errors[0])
        agents, errors = catalog.checkBuild({"agents": [{"name": "A", "task": "author", "model": "claude-sonnet-5-5", "waits_for": ["Leader", "Ghost"], "settings": {"subject": "a"}},
                                                        {"name": "a", "task": "author", "model": "claude-sonnet-5-5", "settings": {"subject": "b"}},
                                                        {"name": "Leader", "task": "author", "model": "claude-sonnet-5-5", "settings": {"subject": "c"}}]})
        self.assertEqual(errors, ["Agent 1 (A): A cannot wait for you, Leader: you work last and receive every result.",
                                  "Agent 1 (A): A cannot wait for Ghost: it is not another agent of the swarm. It can wait for: a, Leader.",
                                  "Agent 2 (a): There is already an agent called a.", "Agent 3 (Leader): Leader is your own name: give the agent another one."])
        agents, errors = catalog.checkBuild({"agents": []})
        self.assertEqual(errors, ["The swarm needs at least one agent."])
        agents, errors = catalog.checkBuild({"agents": [{"name": f"A{number}", "task": "author", "model": "m"} for number in range(catalog.maxAgents + 1)]})
        self.assertIn(f"{catalog.maxAgents} agents at most", errors[0])

    def testTheLeaderIsToldWhatItCanChooseOnThisComputer(self):
        catalog = makeCatalog(self.folder, keys={"claude": "sk"})
        models = catalog.describeModels(19.7)
        self.assertIn("so all the next ones together can take 4.3 GB", models)
        self.assertIn("Qwen/Qwen3.5-0.8B (2.2 GB)", models)
        self.assertNotIn("Qwen/Qwen3-32B", models)
        self.assertIn("- Anthropic (ready, the API key is set): claude-fable-5-1", models)
        self.assertIn("- OpenAI (not ready: the user must give an API key first)", models)
        self.assertIn("- codex: Codex, with the ChatGPT plan of the user (cannot be used: Codex is not installed)", models)
        self.assertIn("- claude-code: Claude Code, billed to the Anthropic API key of the user (ready)", models)
        self.assertIn("None can be used: no supported GPU was found", makeCatalog(self.folder, machine(gpus=[])).describeModels())
        tasks = leader_catalog.describeTasks()
        self.assertIn("    filePath (the path of a file that exists in the folder; required): Path of the text to check", tasks)
        self.assertIn("    telegramToken (a secret: never write it, the user gives it; only when messenger is Telegram; required)", tasks)
        self.assertIn("    length (a whole number; default: 300): Maximum number of words", tasks)
        self.assertIn("The publishers: Springer Nature, Elsevier", tasks)
        self.assertNotIn("leader", re.findall(r"^- (\w+):", tasks, re.M))
        self.assertEqual(agent_storehouse.describeTree(self.folder.resolve()).splitlines(), ["- paper.tex (29 bytes)", "- notes/ideas.md (5 bytes)"])
        prompt = catalog.buildPrompt()
        for part in ("You are Leader, the leader of a swarm", "Check my paper and write a summary of it.", str(self.folder.resolve()), "<swarmup_build>"):
            self.assertIn(part, prompt)


class BudgetTests(FolderTestCase):
    def testTheLeaderIsOnlyOfferedTheModelsThatFitWhatIsLeftOfTheBudget(self):
        catalog = makeCatalog(self.folder, keys={"claude": "sk", "gpt": "sk"})
        catalog.costs.setBudget(40)
        models = catalog.describeModels(0, catalog.moneyLeft(catalog.leaderTeam()))
        self.assertIn("$15.00 of the budget of the mission is left for new models.", models)
        self.assertIn("claude-sonnet-5-5 ($15.00 per 1M tokens)", models)
        self.assertNotIn("claude-opus-5-5", models.split("RECOMMENDED")[0].split("API MODELS")[1])
        self.assertIn("gemini-3.8-flash (price unknown)", models)
        self.assertIn("other API models cost more than what is left of the budget", models)
        self.assertIn("Its use is paid by the plan, not by the budget.", models)
        costs = catalog.describeCosts(catalog.leaderTeam())
        self.assertIn("The budget of the mission is $40.00. The agents that did not finish set aside $25.00", costs)
        prompt = catalog.buildPrompt()
        self.assertIn("THE COST OF THE MISSION", prompt)
        self.assertIn(f"with {catalog.maxAgents} agents at most", prompt)

    def testTheModelsOfTheSwarmAreCheckedAgainstTheBudgetTogether(self):
        catalog = makeCatalog(self.folder)
        catalog.costs.setBudget(49)
        agents, errors = catalog.checkBuild({"agents": [{"name": "A", "task": "author", "model": "claude-sonnet-5-5", "settings": {"subject": "a"}},
                                                        {"name": "B", "task": "author", "model": "claude-haiku-4-5", "settings": {"subject": "b"}},
                                                        {"name": "C", "task": "author", "model": "claude-haiku-4-5", "settings": {"subject": "c"}}]})
        self.assertEqual(errors, ["Agent 3 (C): claude-haiku-4-5 costs $5.00 per 1 million tokens, but only $4.00 is left of the budget of the mission for new models. "
                                  "Choose a cheaper model, a local model, or Codex."])
        agents, errors = catalog.checkBuild({"agents": [{"name": "A", "task": "author", "model": "Qwen/Qwen3-8B", "settings": {"subject": "a"}},
                                                        {"name": "B", "task": "author", "model": "gemini-3.8-flash", "settings": {"subject": "b"}}]})
        self.assertEqual(errors, [])
        self.assertEqual([agent["price"] for agent in agents], ["free", "price unknown"])

    def testWhileTheSwarmRunsTheLeaderSeesWhatEveryAgentSpent(self):
        swarm = Swarm("Mission")
        swarm.addAgent("Leader", makeLeader(LeaderModel()), "leader", "lead", model=getModelInfo("claude-opus-5-5"))
        writer = GatedLoop("text")
        writer.agent.usage = {"calls": 0, "input": 0, "output": 0}
        mission_costs.MissionCosts.track(swarm.costs, "Writer", getModelInfo("claude-haiku-4-5"), writer.agent)
        swarm.addAgent("Writer", writer, "writer", "write", model=getModelInfo("claude-haiku-4-5"))
        from model_support import recordCall
        recordCall(writer.agent.usage, input=100000, output=20000)
        catalog = makeCatalog(self.folder)
        catalog.costs = swarm.costs
        swarm.costs.setBudget(30)
        for name in ("claude-opus-5-5", "claude-haiku-4-5"):
            swarm.costs.priceOf(getModelInfo(name))
        text = catalog.describeCosts(swarm.describeTeam())
        self.assertIn("The mission spent $0.20 so far.", text)
        self.assertIn("- Writer: $0.20 (1 calls, 100,000 tokens read, 20,000 written)", text)
        self.assertIn("so $0.00 is left for new models. The budget is spent", text)
        problem = catalog.checkUsable(getModelInfo("claude-haiku-4-5"), 0, {}, catalog.moneyLeft(swarm.describeTeam()))
        self.assertIn("only $0.00 is left of the budget", problem)
        self.assertEqual(catalog.checkUsable(getModelInfo("Qwen/Qwen3-8B"), 0, {}, catalog.moneyLeft(swarm.describeTeam())), "")


class DesignTests(FolderTestCase):
    GOOD = [{"name": "Checker", "task": "math", "model": "claude-sonnet-5-5", "settings": {"filePath": "paper.tex"}, "why": "It checks the proofs."},
            {"name": "Writer", "task": "author", "model": "claude-haiku-4-5", "waits_for": ["Checker"], "settings": {"subject": "a summary of the paper", "length": 200},
             "why": "It writes the summary."}]

    def testTheLeaderWritesItsSwarmAgainUntilItCanBeUsedAndTheUserApprovesIt(self):
        model = LeaderModel(build=["I think two agents are enough, but here is no block."], repair=[block("build", agents=[{**self.GOOD[0], "model": "gpt-9"}]),
                                                                                                     "Fixed.\n" + block("build", agents=self.GOOD)])
        leader = makeLeader(model)
        agents = designSwarm(leader, makeCatalog(self.folder))
        self.assertEqual([(agent["name"], agent["task"], agent["model"]["name"], agent["waitsFor"]) for agent in agents],
                         [("Checker", "math", "claude-sonnet-5-5", []), ("Writer", "author", "claude-haiku-4-5", ["Checker"])])
        self.assertEqual(model.kinds(), ["build", "repair", "repair"])
        self.assertIn("Your answer has no <swarmup_build> block", model.prompts[1])
        self.assertIn("'gpt-9' is not a model of the list of models", model.prompts[2])
        [proposal] = leader.proposals
        self.assertEqual((proposal["action"], proposal["why"], [agent["name"] for agent in proposal["agents"]]), ("build", "Fixed.", ["Checker", "Writer"]))
        self.assertIn("1. Checker: Math checker, with claude-sonnet-5-5 (Anthropic API), $15.00 per 1M tokens. It starts right away.", proposal["summary"])
        self.assertIn("   Why: It writes the summary.", proposal["summary"])
        self.assertEqual(agents[1]["answers"]["folder"], str(self.folder.resolve()))
        self.assertEqual(len(leader.notes), 2)

    def testTheUserSaysWhatToChangeAndTheLeaderWritesTheWholeSwarmAgain(self):
        model = LeaderModel(build=[block("build", agents=self.GOOD)], revise=[block("build", agents=self.GOOD[:1])])
        leader = makeLeader(model, decisions=[{"decision": "reject", "message": "Only the checker, please."}])
        agents = designSwarm(leader, makeCatalog(self.folder))
        self.assertEqual([agent["name"] for agent in agents], ["Checker"])
        self.assertIn("The user read the swarm you proposed and wants changes: Only the checker, please.", model.prompts[1])
        self.assertIn('"name": "Writer"', model.prompts[1])

    def testARejectedSwarmBuildsNothing(self):
        leader = makeLeader(LeaderModel(build=[block("build", agents=self.GOOD)]), decisions=[{"decision": "reject", "message": ""}])
        self.assertIsNone(designSwarm(leader, makeCatalog(self.folder)))

    def testALeaderThatNeverWritesAUsableSwarmStopsWithTheReason(self):
        model = LeaderModel(build=["No."], repair=["Still no."] * 5)
        with self.assertRaises(ValueError) as caught:
            designSwarm(makeLeader(model), makeCatalog(self.folder))
        self.assertIn("The leader could not write a swarm that SwarmUP can use", str(caught.exception))
        self.assertEqual(len(model.prompts), leader_utils.REPAIR_ATTEMPTS + 1)

    def testWhatOnlyTheUserKnowsIsAskedOnceTheSwarmIsApprovedAndCheckedAgain(self):
        mailer = {"name": "Mailer", "task": "email", "model": "gpt-6-luna", "settings": {"provider": "Gmail", "to": "prof@uni.edu", "subject": "Summary", "request": "Send it"}}
        catalog = makeCatalog(self.folder, keys={})
        answers = [{"Mailer::sender": ["not an address"], "Mailer::password": ["app-password-1"], "key::gpt": ["sk-openai"]}, {"Mailer::sender": ["me@uni.edu"]}]
        leader = makeLeader(LeaderModel(build=[block("build", agents=[mailer])]), answers=answers)
        [agent] = designSwarm(leader, catalog)
        self.assertEqual((agent["answers"]["sender"], agent["answers"]["password"], agent["answers"]["smtp"]), ("me@uni.edu", "app-password-1", "smtp.gmail.com"))
        self.assertEqual(catalog.keys, {"gpt": "sk-openai"})
        first, second = leader.questions
        self.assertEqual([(question["id"], question["secret"]) for question in first], [("Mailer::sender", False), ("Mailer::password", True), ("key::gpt", True)])
        self.assertEqual([question["id"] for question in second], ["Mailer::sender"])
        self.assertIn("This is not an email address", second[0]["question"])


# A program that runs the swarm, as LeaderManager needs it: the agents it makes are gated loops.
class Maker:
    def __init__(self):
        self.made, self.names, self.models = [], [], []

    def makeLoop(self, agent):
        loop = GatedLoop(f"{agent['name']} made it")
        self.made.append(agent)
        return loop

    def joined(self, agent, loop):
        self.names.append(agent["name"])

    def remakeLoop(self, name, model):
        return GatedLoop(f"{name} with {model['name']}")

    def remade(self, name, model, loop):
        self.models.append((name, model["name"]))


class ManagerTests(FolderTestCase):
    def setUp(self):
        super().setUp()
        self.events = []

    def build(self, model, decisions=(), gate=None, extra=()):
        leader = makeLeader(model, decisions)
        swarm = Swarm("Check my paper and write a summary of it.")
        swarm.addAgent("Leader", leader, "leader", "lead", model=getModelInfo("claude-opus-5-5"))
        swarm.addAgent("Checker", GatedLoop("checked"), "math checker", "check", model=getModelInfo("claude-sonnet-5-5"))
        swarm.addAgent("Writer", GatedLoop("written", gate), "writer", "write", model=getModelInfo("claude-haiku-4-5"))
        for name, waiting in extra:
            swarm.addAgent(name, GatedLoop(name.lower(), waiting), "writer", "write", waitsFor=["Writer"], model=getModelInfo("claude-haiku-4-5"))
        self.maker = Maker()
        self.manager = LeaderManager(swarm, makeCatalog(self.folder), self.maker)
        swarm.addListener(self.events.append)
        return swarm, leader

    def start(self, swarm, mode="execute"):
        swarm.setMode(mode)
        swarm.startInBackground()
        self.addCleanup(lambda: (swarm.stopWork() if swarm.isRunning() else None, swarm.wait(10)))

    def testWhenAnAgentIsDoneTheLeaderIsAskedAndTheAgentItProposesJoinsOnceTheUserApproves(self):
        gate = threading.Event()
        add = block("add", name="Translator", task="author", model="claude-haiku-4-5", waits_for=["Checker"], settings={"subject": "the summary in French"},
                    why="The user wants it in French too.")
        model = LeaderModel(supervise=[add])
        swarm, leader = self.build(model, gate=gate)
        self.start(swarm)
        waitUntil(lambda: "Translator" in swarm.getAgents(), "the translator to join")
        gate.set()
        self.assertEqual(swarm.wait(10)["result"], "Report: every agent did its work.")
        [proposal] = leader.proposals
        self.assertEqual((proposal["action"], proposal["agent"], proposal["why"]), ("add", "Translator", "The user wants it in French too."))
        self.assertEqual(proposal["details"]["waitsFor"], ["Checker"])
        self.assertEqual(self.maker.names, ["Translator"])
        self.assertEqual(swarm.getInfo("Translator")["result"], "Translator made it")
        self.assertEqual([message["message"] for message in swarm.getMessages("Checker", "Translator")], ["checked"])
        self.assertIn("Checker is done.", model.prompts[model.kinds().index("supervise")])
        self.assertIn("SwarmUP: The user approved your proposal, and it is done: Add Translator", "\n".join(leader.inbox))
        self.assertIn("Translator joined the swarm", "\n".join(swarm.getMember("Writer")["agent"].inbox))
        self.assertEqual(swarm.getMember("Translator")["recipe"]["task"], "author")

    def testARemovalNeedsTheApprovalOfTheUserAndKeepsTheReasonOfTheLeader(self):
        gate = threading.Event()
        model = LeaderModel(supervise=[block("remove", name="Checker", why="Its check is done, and its model can free the GPU.")])
        swarm, leader = self.build(model, gate=gate)
        self.start(swarm)
        waitUntil(lambda: "Checker" not in swarm.getAgents(), "the checker to leave")
        gate.set()
        swarm.wait(10)
        self.assertEqual(swarm.removed[0]["reason"], "Its check is done, and its model can free the GPU.")
        self.assertEqual(leader.proposals[0]["details"]["status"], "done")
        self.assertIn("Checker (math checker) left the swarm. Why: Its check is done", "\n".join(swarm.getMember("Writer")["agent"].inbox))

    def testARejectedProposalIsToldToTheLeaderAndNeverShownAgain(self):
        gate = threading.Event()
        remove = block("remove", name="Checker", why="Done.")
        model = LeaderModel(supervise=[remove, remove])
        last = threading.Event()
        swarm, leader = self.build(model, decisions=[{"decision": "reject", "message": "Keep it, I may need it."}], gate=gate, extra=[("Third", last)])
        self.start(swarm)
        waitUntil(lambda: model.kinds().count("supervise") >= 1 and leader.proposals, "the first proposal")
        gate.set()
        waitUntil(lambda: model.kinds().count("supervise") == 2, "the second supervision")
        waitUntil(lambda: "already rejected" in "\n".join(leader.inbox), "the leader to be told")
        last.set()
        swarm.wait(10)
        self.assertEqual(len(leader.proposals), 1)
        self.assertIn("Checker", swarm.getAgents())
        inbox = "\n".join(leader.inbox)
        self.assertIn("The user rejected your proposal: Remove Checker (math checker, done). The user said: Keep it, I may need it.", inbox)
        second = model.prompts[[index for index, kind in enumerate(model.kinds()) if kind == "supervise"][1]]
        self.assertIn("- Rejected: Remove Checker (math checker, done). The user said: Keep it, I may need it.", second)

    def testASentenceThatSuggestsAChangeIsConfirmedByTheLeaderBeforeTheUserIsAsked(self):
        gate = threading.Event()
        model = LeaderModel(supervise=["Checker is no longer needed, its check is done."], confirm=[block("remove", name="Checker", why="Its check is done.")])
        swarm, leader = self.build(model, gate=gate)
        self.start(swarm)
        waitUntil(lambda: "Checker" not in swarm.getAgents(), "the checker to leave")
        gate.set()
        swarm.wait(10)
        confirm = model.prompts[model.kinds().index("confirm")]
        self.assertIn('"Checker is no longer needed, its check is done."', confirm)
        self.assertEqual(len(leader.proposals), 1)

    def testASentenceTheLeaderDoesNotConfirmChangesNothing(self):
        gate = threading.Event()
        model = LeaderModel(supervise=["We should remove Checker."], confirm=["NO CHANGE"])
        swarm, leader = self.build(model, gate=gate)
        self.start(swarm)
        waitUntil(lambda: "confirm" in model.kinds(), "the confirmation")
        gate.set()
        swarm.wait(10)
        self.assertEqual((leader.proposals, "Checker" in swarm.getAgents()), ([], True))

    def testEveryAnswerOfTheLeaderIsReadAndItsBlocksAreTakenOutOfTheTextForTheUser(self):
        swarm, leader = self.build(LeaderModel())
        text = "Where the team stands: Checker, Writer.\n" + block("remove", name="Writer", why="Not needed.")
        self.assertEqual(self.manager.readOutput(text), "Where the team stands: Checker, Writer.")
        kind, found = self.manager.queue.get_nowait()
        self.assertEqual((kind, found["blocks"][0]["data"]), ("output", {"name": "Writer", "why": "Not needed."}))
        self.assertEqual(self.manager.readOutput("Checker is no longer needed."), "Checker is no longer needed.")
        kind, found = self.manager.queue.get_nowait()
        self.assertEqual(([item["action"] for item in found["suggestions"]], found["blocks"]), (["remove"], []))
        for text in ("Where the team stands: all is fine.", '{"Writer": [], "Checker": ["Writer"]}', '{"Writer": "Remove the second paragraph"}'):
            self.assertEqual(self.manager.readOutput(text), text)
        self.assertTrue(self.manager.queue.empty())
        swarm.manager = self.manager
        member = swarm.getMember("Leader")
        swarm.connectAgent("Leader", member, False)
        self.assertEqual(leader.onAnswer, self.manager.readOutput)
        swarm.connectAgent("Writer", swarm.getMember("Writer"), False)
        self.assertIsNone(swarm.getMember("Writer")["agent"].onAnswer)

    def testAChangeThatIsNotPossibleGoesBackToTheLeaderBeforeTheUserIsAsked(self):
        gate = threading.Event()
        model = LeaderModel(supervise=[block("remove", name="Leader", why="x") + block("remove", name="Ghost", why="x")],
                            repair=[block("model", name="Writer", model="claude-sonnet-5-5", why="Better.")])
        swarm, leader = self.build(model, gate=gate)
        self.start(swarm)
        waitUntil(lambda: "repair" in model.kinds(), "the repair round")
        gate.set()
        swarm.wait(10)
        repair = model.prompts[model.kinds().index("repair")]
        self.assertIn("You cannot remove yourself nor change your own model", repair)
        self.assertIn("There is no agent called Ghost in the swarm. Its agents are: Checker, Writer.", repair)
        self.assertEqual(leader.proposals, [])
        self.assertIn("Writer already started, so its model cannot change", "\n".join(leader.inbox))

    def testAChangeWithoutAReasonGoesBackToTheLeader(self):
        catalog = makeCatalog(self.folder)
        swarm, leader = self.build(LeaderModel())
        for action, data in (("remove", {"name": "Checker"}), ("add", {"name": "New", "task": "author", "model": "claude-haiku-4-5", "settings": {"subject": "x"}}),
                             ("model", {"name": "Writer", "model": "claude-sonnet-5-5", "why": " "})):
            change, errors = catalog.checkChange(action, data, swarm)
            self.assertIsNone(change)
            self.assertIn('Every change needs "why"', errors[0])

    def testTheModelOfAnAgentThatDidNotStartCanChange(self):
        gate = threading.Event()
        model = LeaderModel(supervise=[block("model", name="Third", model="claude-sonnet-5-5", why="It needs more depth.")])
        swarm, leader = self.build(model, gate=gate, extra=[("Third", None)])
        self.start(swarm)
        waitUntil(lambda: self.maker.models, "the new model")
        gate.set()
        swarm.wait(10)
        self.assertEqual(self.maker.models, [("Third", "claude-sonnet-5-5")])
        self.assertEqual(swarm.getInfo("Third")["result"], "Third with claude-sonnet-5-5")
        self.assertIn("Change the model of Third from claude-haiku-4-5 (Anthropic API) to claude-sonnet-5-5 (Anthropic API)", leader.proposals[0]["text"])
        self.assertTrue(any(event["kind"] == "model" and event["agent"] == "Third" for event in self.events))

    def testWhatTheUserWritesToTheLeaderIsAReasonToAskIt(self):
        gate = threading.Event()
        model = LeaderModel()
        swarm, leader = self.build(model, gate=gate)
        self.start(swarm)
        waitUntil(lambda: swarm.getStatus("Writer") == "working", "the writer to work")
        waitUntil(lambda: model.kinds().count("supervise") == 1, "the first supervision")
        swarm.sendUserMessage("Leader", "Please also translate it into French.")
        waitUntil(lambda: model.kinds().count("supervise") == 2, "the leader to be asked")
        gate.set()
        swarm.wait(10)
        prompt = model.prompts[[index for index, kind in enumerate(model.kinds()) if kind == "supervise"][1]]
        self.assertIn("- The user wrote to you: Please also translate it into French.", prompt)
        self.assertIn("Messages from the user, sent while you work", prompt)

    def testTheManagerIsSavedWithTheSwarm(self):
        swarm, leader = self.build(LeaderModel())
        self.assertTrue(swarm.describeState()["managed"])
        self.assertIs(swarm.manager, self.manager)


if __name__ == "__main__":
    unittest.main()
