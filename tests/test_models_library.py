import re
import sys
import unittest
from pathlib import Path

# The modules of SwarmUP are in the folders of src/backend. Their names have hyphens, so they are not packages: each folder goes on the path.
sys.path[:0] = [str(folder) for folder in sorted((Path(__file__).resolve().parent.parent / "src" / "backend").iterdir()) if folder.is_dir() and not folder.name.startswith(("_", "."))]
from models_library import (API_KEYS, MODELS_API, MODELS_LOCAL, PRICE_NOTES, PRICING_PAGES, RECOMMENDED_API, RECOMMENDED_LOCAL, getModelInfo, getProvider,
                            getVram, isGated)


class LocalModelTests(unittest.TestCase):
    def allLocal(self):
        return {name: size for family in MODELS_LOCAL.values() for name, size in family.items()}

    def testTheFamiliesNamedInTheDesignAreThere(self):
        for family in ("qwen", "llama", "kimi", "mistral", "deepseek"):
            self.assertGreater(len(MODELS_LOCAL[family]), 2, family)

    def testEveryModelHasAHuggingFaceNameAndASize(self):
        for name, size in self.allLocal().items():
            self.assertRegex(name, r"^[\w.-]+/[\w.-]+$")
            self.assertIsInstance(size, float, name)
            self.assertGreater(size, 0, name)

    def testNoModelIsListedTwice(self):
        names = [name for family in MODELS_LOCAL.values() for name in family]
        self.assertEqual(len(names), len(set(names)))

    def testFamiliesOnlyContainTheirOwnModels(self):
        owners = {"qwen": "Qwen", "llama": "meta-llama", "kimi": "moonshotai", "mistral": "mistralai", "deepseek": "deepseek-ai", "gemma": "google",
                  "phi": "microsoft", "gpt-oss": "openai", "glm": "zai-org", "granite": "ibm-granite", "olmo": "allenai", "nemotron": "nvidia"}
        self.assertEqual(set(owners), set(MODELS_LOCAL))
        for family, names in MODELS_LOCAL.items():
            self.assertTrue(all(name.startswith(owners[family] + "/") for name in names), family)

    def testEveryRecommendationIsInTheListAndSortedFromSmallestToLargest(self):
        models = self.allLocal()
        self.assertEqual(set(RECOMMENDED_LOCAL), {"math", "code", "writing", "email", "calendar", "news", "literature", "formatting", "leading"})
        for task, names in RECOMMENDED_LOCAL.items():
            self.assertTrue(all(name in models for name in names), task)
            sizes = [models[name] for name in names]
            self.assertEqual(sizes, sorted(sizes), task)

    def testVramIsAnEstimateOfTheWeightsPlusOverhead(self):
        self.assertEqual(getVram(8), 19.2)
        self.assertEqual(getVram(8, 4), 4.8)
        self.assertEqual(getVram(70.6), 169.4)
        self.assertLess(getVram(8, 4), getVram(8, 8))
        self.assertLess(getVram(8, 8), getVram(8))


class RecommendationTests(unittest.TestCase):
    def testEveryTaskHasRecommendationsForBothKindsOfModels(self):
        self.assertEqual(set(RECOMMENDED_API), set(RECOMMENDED_LOCAL))
        for task in RECOMMENDED_LOCAL:
            self.assertGreaterEqual(len(RECOMMENDED_LOCAL[task]), 4, task)
            self.assertEqual(len(RECOMMENDED_LOCAL[task]), len(set(RECOMMENDED_LOCAL[task])), task)

    def testEveryApiRecommendationIsListedAndEveryProviderIsRepresented(self):
        listed = {name: provider for provider, models in MODELS_API.items() for name in models}
        for task, names in RECOMMENDED_API.items():
            self.assertTrue(all(name in listed for name in names), task)
            self.assertEqual({listed[name] for name in names}, set(MODELS_API), task)
            self.assertEqual(len(names), len(set(names)), task)

    def testTheHardTasksGetTheMostCapableModelsAndTheLightOnesTheCheapest(self):
        for provider, models in MODELS_API.items():
            best = next(name for name in RECOMMENDED_API["math"] if name in models)
            cheapest = next(name for name in RECOMMENDED_API["calendar"] if name in models)
            self.assertLessEqual(models.index(best), models.index(cheapest), provider)
        self.assertEqual(RECOMMENDED_API["math"], RECOMMENDED_API["code"])
        self.assertEqual(RECOMMENDED_API["calendar"], RECOMMENDED_API["formatting"])

    def testEveryProviderTellsWhereToGetItsKey(self):
        self.assertEqual(set(API_KEYS), set(MODELS_API))
        for provider, details in API_KEYS.items():
            self.assertRegex(details["variable"], r"^[A-Z_]+_API_KEY$", provider)
            self.assertTrue(details["page"].startswith("https://"), provider)
            self.assertTrue(details["company"], provider)
        self.assertEqual(len({details["variable"] for details in API_KEYS.values()}), 4)


class ApiModelTests(unittest.TestCase):
    def testTheFourProvidersOfTheDesignAreThere(self):
        self.assertEqual(set(MODELS_API), {"gpt", "claude", "gemini", "deepseek"})

    def testNamesAreCleanAndNotRepeated(self):
        for provider, names in MODELS_API.items():
            self.assertGreater(len(names), 1, provider)
            self.assertEqual(len(names), len(set(names)), provider)
            self.assertTrue(all(re.fullmatch(r"[a-z0-9][a-z0-9.-]*", name) for name in names), provider)

    def testClaudeNamesAreTheExactNamesOfTheApiWithoutDates(self):
        for name in MODELS_API["claude"]:
            self.assertRegex(name, r"^claude-(fable|opus|sonnet|haiku)-\d+(-\d+)?$")
        self.assertIn("claude-opus-5-5", MODELS_API["claude"])

    def testEveryProviderHasAnOfficialPricingPage(self):
        self.assertEqual(set(PRICING_PAGES), set(MODELS_API))
        self.assertTrue(all(page.startswith("https://") for page in PRICING_PAGES.values()))
        self.assertTrue(set(PRICE_NOTES) <= set(MODELS_API))

    def testTheSmallerGptFourModelsAreThereAndTheShutDownOneIsNot(self):
        for name in ("gpt-4o-mini", "gpt-4.1-mini"):
            self.assertIn(name, MODELS_API["gpt"])
        self.assertNotIn("gpt-4.1-nano", MODELS_API["gpt"])

    def testEachProviderUsesItsOwnNames(self):
        prefixes = {"gpt": "gpt-", "claude": "claude-", "gemini": "gemini-", "deepseek": "deepseek-"}
        for provider, names in MODELS_API.items():
            self.assertTrue(all(name.startswith(prefixes[provider]) for name in names), provider)


class ModelInfoTests(unittest.TestCase):
    def testAListedLocalModelNeedsItsVram(self):
        info = getModelInfo("Qwen/Qwen3.5-9B")
        self.assertEqual(info, {"name": "Qwen/Qwen3.5-9B", "local": True, "provider": None, "billions": 9.7, "bits": 16, "vram": getVram(9.7), "cli": None})
        self.assertEqual(getModelInfo("Qwen/Qwen3.5-9B", bits=4)["vram"], getVram(9.7, 4))
        self.assertLess(getModelInfo("Qwen/Qwen3.5-9B", bits=4)["vram"], info["vram"])

    def testEveryListedModelCanBeChosen(self):
        for family in MODELS_LOCAL.values():
            for name, size in family.items():
                self.assertEqual(getModelInfo(name)["vram"], getVram(size), name)

    def testApiModelsNeedNoVramAndKnowTheirProvider(self):
        for provider, models in MODELS_API.items():
            for name in models:
                info = getModelInfo(name)
                self.assertEqual((info["local"], info["vram"], info["provider"]), (False, 0.0, provider), name)

    def testAnApiModelThatIsNotListedCanBeEnteredWithItsProvider(self):
        self.assertEqual(getModelInfo("claude-future-9")["provider"], "claude")
        self.assertEqual(getModelInfo("gemini-9-ultra")["provider"], "gemini")
        self.assertEqual(getModelInfo("brand-new-model", provider="deepseek")["provider"], "deepseek")
        self.assertEqual(getModelInfo("claude-opus-5-5", provider="gpt")["provider"], "claude")
        with self.assertRaisesRegex(ValueError, "owner/name"):
            getModelInfo("brand-new-model")
        with self.assertRaises(ValueError):
            getModelInfo("brand-new-model", provider="nobody")
        with self.assertRaises(ValueError):
            getModelInfo("", provider="gpt")
        self.assertIsNone(getProvider("Qwen/Qwen3-8B"))
        self.assertEqual(getProvider(" gpt-6-astra "), "gpt")

    def testGatedModelsAreRecognised(self):
        for name in ("meta-llama/Llama-3.2-1B-Instruct", "google/gemma-3-1b-it"):
            self.assertTrue(isGated(name), name)
        for name in ("Qwen/Qwen3-8B", "google/gemma-4-12B-it", "microsoft/phi-4"):
            self.assertFalse(isGated(name), name)
        self.assertTrue(all(isGated(name) for name in MODELS_LOCAL["llama"]))

    def testAModelFromHuggingFaceThatIsNotListedNeedsItsSize(self):
        with self.assertRaisesRegex(ValueError, "number of parameters"):
            getModelInfo("someone/custom-7b")
        info = getModelInfo(" someone/custom-7b ", billions=7.0)
        self.assertEqual((info["name"], info["local"], info["vram"]), ("someone/custom-7b", True, getVram(7.0)))
        with self.assertRaises(ValueError):
            getModelInfo("someone/custom-7b", billions=-1)

    def testANameThatIsNeitherListedNorFromHuggingFaceIsRefused(self):
        with self.assertRaisesRegex(ValueError, "owner/name"):
            getModelInfo("Qwen3-8B")

    def testOnlyTheKnownPrecisionsAreAccepted(self):
        for bits in (16, 8, 4):
            self.assertTrue(getModelInfo("Qwen/Qwen3.5-9B", bits=bits)["local"])
        for bits in (0, 5, 32):
            with self.assertRaises(ValueError):
                getModelInfo("Qwen/Qwen3.5-9B", bits=bits)


if __name__ == "__main__":
    unittest.main()
