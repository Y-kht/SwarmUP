import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
# The modules of SwarmUP are in the folders of src/backend. Their names have hyphens, so they are not packages: each folder goes on the path.
sys.path[:0] = [str(folder) for folder in sorted((Path(__file__).resolve().parent.parent / "src" / "backend").iterdir()) if folder.is_dir() and not folder.name.startswith(("_", "."))]
import harness_utils
import model_clients
from harness_utils import MissionCosts, Swarm, callCost, checkBudget, formatDollars, isDeepseekPeak, priceOfModel
from model_clients import ClaudeCodeModel, readOpenAiUsage, recordCall
from models_library import getModelInfo
from test_leader_utils import fixedPrices, usePrices
from test_live_swarm import GatedLoop

SONNET = {"kind": "priced", "provider": "claude", "input": 3.0, "output": 15.0, "cachedInput": 0.3, "cacheWrite": 3.75, "inputLong": 6.0, "outputLong": 22.5,
          "cachedInputLong": 0.6, "cacheWriteLong": 7.5, "reference": 15.0}


def call(input=0, cachedInput=0, cacheWrite=0, output=0, cost=None, time="2026-10-05T12:00:00", missing=False):
    return {"time": time, "input": input, "cachedInput": cachedInput, "cacheWrite": cacheWrite, "output": output, "cost": cost, "missing": missing}


# A client of a model as the mission sees it: its usage, with the record of every call.
class Client:
    def __init__(self, *records, accountType=None):
        self.usage = {"calls": 0, "input": 0, "output": 0}
        for record in records:
            recordCall(self.usage, **{key: value for key, value in record.items() if key != "time"})
        if accountType:
            self.accountType = accountType


class CallCostTests(unittest.TestCase):
    def testEveryKindOfTokenHasItsOwnPrice(self):
        self.assertAlmostEqual(callCost(call(input=1000, output=500), SONNET), (1000 * 3.0 + 500 * 15.0) / 1e6)
        self.assertAlmostEqual(callCost(call(input=100, cachedInput=10000, cacheWrite=2000, output=50), SONNET), (100 * 3.0 + 10000 * 0.3 + 2000 * 3.75 + 50 * 15.0) / 1e6)

    def testALongPromptIsBilledAtTheLongPriceWhenTheModelHasOne(self):
        self.assertAlmostEqual(callCost(call(input=150000, cachedInput=60000, output=1000), SONNET), (150000 * 6.0 + 60000 * 0.6 + 1000 * 22.5) / 1e6)
        self.assertAlmostEqual(callCost(call(input=200000, output=1000), SONNET), (200000 * 3.0 + 1000 * 15.0) / 1e6)
        plain = {**SONNET, "inputLong": None, "outputLong": None, "cachedInputLong": None, "cacheWriteLong": None}
        self.assertAlmostEqual(callCost(call(input=300000, output=1000), plain), (300000 * 3.0 + 1000 * 15.0) / 1e6)

    def testAMissingCachePriceIsTheReadingPrice(self):
        price = {**SONNET, "cachedInput": None, "cacheWrite": None}
        self.assertAlmostEqual(callCost(call(cachedInput=1000, cacheWrite=1000), price), 2000 * 3.0 / 1e6)

    def testDeepseekCostsHalfOutsideItsPeakHours(self):
        price = {"kind": "priced", "provider": "deepseek", "input": 0.5, "output": 2.0, "cachedInput": 0.05, "reference": 2.0}
        peak, night, weekend = "2026-10-05T07:30:00", "2026-10-05T12:00:00", "2026-10-03T07:30:00"
        self.assertEqual([isDeepseekPeak(moment) for moment in (peak, night, weekend, "2026-10-05T04:00:00", "2026-10-05T01:00:00")], [True, False, False, False, True])
        full = (1000 * 0.5 + 1000 * 2.0) / 1e6
        self.assertAlmostEqual(callCost(call(input=1000, output=1000, time=peak), price), full)
        self.assertAlmostEqual(callCost(call(input=1000, output=1000, time=night), price), full / 2)
        self.assertAlmostEqual(callCost(call(input=1000, output=1000, time=weekend), price), full / 2)

    def testWhatAModelBilledItselfIsTakenAsItIsAndWhatIsNotKnownIsNeverFree(self):
        self.assertEqual(callCost(call(input=10, output=10, cost=0.42), SONNET), 0.42)
        self.assertEqual(callCost(call(input=10, output=10, cost=0.42), {"kind": "unknown", "reference": None}), 0.42)
        self.assertIsNone(callCost(call(input=10, output=10), {"kind": "unknown", "reference": None}))
        self.assertIsNone(callCost(call(missing=True), SONNET))
        self.assertEqual(callCost(call(input=10, output=10), {"kind": "free", "reference": 0.0}), 0.0)
        self.assertEqual(callCost(call(input=10, output=10), {"kind": "plan", "reference": 0.0}), 0.0)

    def testTheBudgetAndTheAmountsAreWrittenForPeople(self):
        self.assertEqual([checkBudget(value) for value in ("5", "$12.50", "1,200", 3, "", None)], [5.0, 12.5, 1200.0, 3.0, None, None])
        for wrong in ("0", "-2", "ten", "nan", "inf"):
            with self.assertRaises(ValueError):
                checkBudget(wrong)
        self.assertEqual([formatDollars(value) for value in (0, 0.0042, 1.5, 1234.5, None)], ["$0.00", "$0.0042", "$1.50", "$1,234.50", "unknown"])


class TokenTests(unittest.TestCase):
    def testTheTokensOfTheOpenAiApisAreReadTheWayTheyAreBilled(self):
        openai = SimpleNamespace(prompt_tokens=1000, completion_tokens=300, total_tokens=1300, prompt_tokens_details=SimpleNamespace(cached_tokens=400))
        self.assertEqual(readOpenAiUsage(openai), {"input": 600, "cachedInput": 400, "output": 300})
        deepseek = SimpleNamespace(prompt_tokens=1000, completion_tokens=300, total_tokens=1300, prompt_tokens_details=None, prompt_cache_hit_tokens=700)
        self.assertEqual(readOpenAiUsage(deepseek), {"input": 300, "cachedInput": 700, "output": 300})
        gemini = SimpleNamespace(prompt_tokens=1000, completion_tokens=200, total_tokens=1900, prompt_tokens_details=None)
        self.assertEqual(readOpenAiUsage(gemini), {"input": 1000, "cachedInput": 0, "output": 900})
        self.assertEqual(readOpenAiUsage(None), {"missing": True})

    def testEveryCallIsRecordedWithItsTimeInUtc(self):
        usage = {"calls": 0, "input": 0, "output": 0}
        recordCall(usage, input=100, cachedInput=50, cacheWrite=25, output=10)
        recordCall(usage, input=None, output=-5, missing=True)
        self.assertEqual((usage["calls"], usage["input"], usage["output"]), (2, 175, 10))
        self.assertEqual({key: usage["records"][1][key] for key in ("input", "output", "missing")}, {"input": 0, "output": 0, "missing": True})
        self.assertRegex(usage["records"][0]["time"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")

    def testClaudeCodeGivesItsOwnCostAndTheTokensOfEveryModelItUsed(self):
        model = ClaudeCodeModel("default", "sk")
        result = SimpleNamespace(total_cost_usd=0.0731, usage={"input_tokens": 5, "output_tokens": 7},
                                 model_usage={"claude-sonnet-5-5": {"inputTokens": 1200, "outputTokens": 300, "cacheReadInputTokens": 9000, "cacheCreationInputTokens": 400, "costUSD": 0.07},
                                              "claude-haiku-4-5": {"inputTokens": 200, "outputTokens": 20, "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0, "costUSD": 0.0031}})
        model.recordResult(result)
        [record] = model.usage["records"]
        self.assertEqual({key: record[key] for key in ("input", "cachedInput", "cacheWrite", "output", "cost")},
                         {"input": 1400, "cachedInput": 9000, "cacheWrite": 400, "output": 320, "cost": 0.0731})
        model.recordResult(SimpleNamespace(total_cost_usd=None, usage={"input_tokens": 5, "output_tokens": 7}, model_usage={"m": {"inputTokens": 5, "outputTokens": 7, "costUSD": 0.002}}))
        self.assertEqual(model.usage["records"][1]["cost"], 0.002)
        model.recordResult(SimpleNamespace(total_cost_usd=None, usage={"input_tokens": 5, "output_tokens": 7}, model_usage=None))
        self.assertEqual((model.usage["records"][2]["cost"], model.usage["records"][2]["input"]), (None, 5))


class MissionCostTests(unittest.TestCase):
    def setUp(self):
        usePrices(self)

    def testThePriceOfAModelDependsOnHowItRuns(self):
        self.assertEqual(priceOfModel(getModelInfo("Qwen/Qwen3-8B"))["kind"], "free")
        self.assertEqual(priceOfModel(getModelInfo("default", cli="codex"))["kind"], "plan")
        self.assertEqual(priceOfModel(getModelInfo("gpt-6-luna", cli="codex"), SimpleNamespace(accountType="apiKey"))["reference"], 10.0)
        self.assertEqual(priceOfModel(getModelInfo("default", cli="codex"), SimpleNamespace(accountType="apiKey"))["kind"], "unknown")
        self.assertEqual(priceOfModel(getModelInfo("default", cli="codex"), SimpleNamespace(accountType="chatgpt", planType="plus"))["kind"], "plan")
        self.assertEqual(priceOfModel(getModelInfo("default", cli="codex"), SimpleNamespace(accountType="chatgpt", planType="enterprise_cbp_usage_based"))["kind"], "unknown")
        self.assertEqual(priceOfModel(getModelInfo("default", cli="claude-code"))["kind"], "unknown")
        self.assertEqual(priceOfModel(getModelInfo("claude-haiku-4-5", cli="claude-code"))["reference"], 5.0)
        self.assertEqual(priceOfModel(getModelInfo("claude-opus-5-5"))["reference"], 25.0)
        self.assertEqual(priceOfModel(getModelInfo("gemini-3.8-flash"))["kind"], "unknown")

    def testTheSpendingOfEveryClientIsAddedAgentByAgent(self):
        costs = MissionCosts(10)
        writer, again = Client(call(input=1000, output=1000)), Client(call(input=2000, output=0))
        costs.track("Writer", getModelInfo("claude-haiku-4-5"), writer)
        costs.track("Writer", getModelInfo("claude-haiku-4-5"), writer)
        costs.track("Writer", getModelInfo("claude-sonnet-5-5"), again)
        costs.track("Local", getModelInfo("Qwen/Qwen3-8B"), SimpleNamespace(usage={"calls": 3, "input": 900, "output": 90}))
        costs.track("Guess", getModelInfo("gemini-3.8-flash"), Client(call(input=100, output=50)))
        fresh = MissionCosts()
        fresh.track("Writer", getModelInfo("claude-haiku-4-5"), Client(call(input=1000, output=1000)))
        first = fresh.report()
        self.assertEqual((first["pending"], first["unpriced"], first["spent"]), (True, 2000, 0.0))
        for name in ("claude-haiku-4-5", "claude-sonnet-5-5", "gemini-3.8-flash"):
            costs.priceOf(getModelInfo(name))
        report = costs.report()
        rows = {row["agent"]: row for row in report["agents"]}
        self.assertAlmostEqual(rows["Writer"]["cost"], (1000 * 1.0 + 1000 * 5.0 + 2000 * 3.0) / 1e6)
        self.assertEqual((rows["Writer"]["calls"], rows["Writer"]["models"]), (2, ["claude-haiku-4-5", "claude-sonnet-5-5"]))
        self.assertEqual((rows["Local"]["cost"], rows["Local"]["unpriced"]), (0.0, 0))
        self.assertEqual((rows["Guess"]["cost"], rows["Guess"]["unpriced"]), (0.0, 150))
        self.assertEqual(report["unpriced"], 150)
        self.assertAlmostEqual(report["spent"], 0.012)
        costs.rename("Writer", "Author")
        self.assertIn("Author", {row["agent"] for row in costs.report()["agents"]})

    def testEveryModelOfTheTeamSetsAsideThePriceOfAMillionTokensUntilItFinishes(self):
        costs = MissionCosts(50)
        busy = Client(call(input=1000000, output=0))
        costs.track("Busy", getModelInfo("claude-sonnet-5-5"), busy)
        team = [{"agent": "Busy", "model": getModelInfo("claude-sonnet-5-5"), "finished": False},
                {"agent": "Waiting", "model": getModelInfo("claude-opus-5-5"), "finished": False},
                {"agent": "Local", "model": getModelInfo("Qwen/Qwen3-8B"), "finished": False},
                {"agent": "Codex", "model": getModelInfo("default", cli="codex"), "finished": False}]
        for member in team:
            costs.priceOf(member["model"])
        left = costs.left(team)
        self.assertEqual((left["spent"], left["setAside"], left["left"]), (3.0, 12.0 + 25.0, 50 - 3.0 - 37.0))
        busy.usage["records"].append(call(input=0, output=1000000))
        left = costs.left(team)
        self.assertEqual((left["spent"], left["setAside"], left["left"]), (18.0, 25.0, 7.0))
        team[1]["finished"] = True
        self.assertEqual(costs.left(team)["left"], 32.0)
        self.assertEqual(costs.left(team[:1])["left"], 32.0)
        unknown = costs.left([{"agent": "Code", "model": getModelInfo("default", cli="claude-code"), "finished": False}])
        self.assertEqual((unknown["setAside"], unknown["unknown"]), (0.0, ["Code"]))
        self.assertIsNone(MissionCosts().left(team)["left"])

    def testAPriceChangeNeverChangesTheCostOfTheCallsAlreadyMade(self):
        costs = MissionCosts()
        client = Client(call(input=1000000))
        costs.track("A", getModelInfo("claude-haiku-4-5"), client)
        costs.priceOf(getModelInfo("claude-haiku-4-5"))
        self.assertEqual(costs.report()["spent"], 1.0)
        with mock.patch.object(harness_utils, "PRICE_REFRESH_SECONDS", 0):
            with mock.patch.object(harness_utils, "loadModelPrices", lambda: {**fixedPrices(), "claude-haiku-4-5": {**fixedPrices()["claude-haiku-4-5"], "input": 2.0}}):
                client.usage["records"].append(call(input=1000000))
                self.assertEqual(costs.report(wait=True)["spent"], 1.0 + 2.0)
        self.assertEqual(costs.state()["clients"][0]["usage"]["records"][0]["billed"], 1.0)

    def testTheUserIsWarnedOnceAt80AndOnceAt100Percent(self):
        costs = MissionCosts(1)
        client = Client(call(input=850000))
        costs.track("A", getModelInfo("claude-haiku-4-5"), client)
        costs.priceOf(getModelInfo("claude-haiku-4-5"))
        self.assertEqual(len(costs.warnings()), 1)
        self.assertEqual(costs.warnings(), [])
        client.usage["records"].append(call(input=200000))
        [warning] = costs.warnings()
        self.assertIn("all of its budget of $1.00", warning)
        costs.setBudget(5)
        self.assertEqual(costs.warnings(), [])

    def testAMissionThatGoesOnAfterAStopKnowsWhatItSpentBefore(self):
        swarm = Swarm("Mission")
        swarm.costs.setBudget(20)
        swarm.addAgent("Leader", GatedLoop("final"), "leader", "lead", model=getModelInfo("claude-haiku-4-5"))
        swarm.getMember("Leader")["agent"].agent = Client(call(input=1000000, output=100000, time="2026-10-05T07:00:00"))
        swarm.costs.entries[0]["client"] = swarm.getMember("Leader")["agent"].agent
        state = swarm.describeState()
        self.assertEqual(state["costs"]["budget"], 20.0)
        self.assertNotIn("sk", str(state["costs"]))
        restored = Swarm.restore(state, lambda name, data: GatedLoop("final"))
        restored.costs.priceOf(getModelInfo("claude-haiku-4-5"))
        report = restored.getCosts()
        self.assertEqual(report["budget"], 20.0)
        self.assertAlmostEqual(report["spent"], 1.0 + 0.5)
        self.assertEqual([row["calls"] for row in report["agents"]], [1])


class InternetCacheTests(unittest.TestCase):
    def setUp(self):
        from test_harness_utils import isolateInternetCache
        isolateInternetCache(self)

    def testThePublishersOfCrossrefAreKeptAndGivenWithoutInternet(self):
        answers = [b'{"message": {"items": [{"primary-name": "Oxford University Press", "id": 286, "counts": {"total-dois": 5}}]}}']
        def fetch(url, login=None, limit=0):
            if not answers:
                raise OSError("no network")
            return answers.pop(0)
        with mock.patch.object(harness_utils, "fetchUrl", fetch):
            first = harness_utils.findPublishers("Oxford")
            harness_utils.internetCache["crossref-publishers-oxford"]["fetchedAt"] -= harness_utils.PUBLISHERS_REFRESH_SECONDS + 1
            self.assertEqual(harness_utils.findPublishers(" oxford "), first)
        self.assertEqual(first[0]["id"], 286)
        self.assertTrue(harness_utils.describeCached("crossref-publishers-oxford")["offline"])

    def testTheModelsOfCodexAreAskedEveryTimeAndTheLastListIsGivenWhenCodexCannotAnswer(self):
        answers = [[{"id": "gpt-6-luna"}], [{"id": "gpt-6-luna"}, {"id": "gpt-6.1-sol"}]]
        class Connection:
            def request(self, method, params):
                if not answers:
                    raise model_clients.ModelError("Codex cannot reach OpenAI.")
                return {"data": answers.pop(0)}
            def close(self):
                pass
        with mock.patch.object(model_clients, "openCodex", lambda: Connection()):
            self.assertEqual([model["id"] for model in model_clients.listCodexModels()], ["gpt-6-luna"])
            self.assertEqual([model["id"] for model in model_clients.listCodexModels()], ["gpt-6-luna", "gpt-6.1-sol"])
            harness_utils.internetCache["codex-models"]["failedAt"] = 0
            self.assertEqual([model["id"] for model in model_clients.listCodexModels()], ["gpt-6-luna", "gpt-6.1-sol"])


if __name__ == "__main__":
    unittest.main()
