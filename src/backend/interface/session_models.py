import os
import threading
import webbrowser

from base_loop import Loop
from codex_agent import CodexLogin, checkCodex, listCodexModels, readCodexAccount
from gpu_check import checkVram, readGpus
from harness_utils import FETCH_ERRORS, describeError
from interface_views import FormError, describeDownload, describeModel, describePrice
from internet_cache import getModelCost
from mission_costs import formatDollars
from model_support import ModelError, findMissingPackages, getApiKey, isDownloaded, lookupHuggingFace
from models_library import API_KEYS, BITS, DEFAULT_CLI_MODEL, MODELS_API, MODELS_CLI, MODELS_LOCAL, RECOMMENDED_API, RECOMMENDED_LOCAL, getModelInfo, isGated
from swarm_harness import Swarm
from tasks_library import TASKS, buildLoop, describeLoop, getTask, publicAnswers


TEST_PROMPT = "Reply with the single word OK."


# The models of the agents (Session in session_core.py): the lists to choose from with the checks of the GPUs and of the budget,
# the agents that join a running swarm, and the sign-in of Codex.
class SessionModels:
    # ---------- Step 3: the model of each agent. ----------
    # A swarm with the models of the other agents, to check a model with the same rules as the swarm that will run.
    def sizingSwarm(self, excluding=None):
        swarm = Swarm(self.mission or "sizing")
        for spec in self.specs:
            if spec["id"] != excluding and spec["model"]:
                swarm.members[spec["name"]] = swarm.newMember(spec["name"], Loop(None), "", "", None, [], spec["model"], None)
        return swarm

    def localEntry(self, sizing, name, bits, billions=None):
        info = getModelInfo(name, bits=bits, billions=billions)
        check = sizing.checkModel(info)
        status = "fits" if check["allowed"] and not check["message"] else "busy" if check["allowed"] else "tooBig"
        return {"name": name, "billions": info["billions"], "vram": info["vram"], "bits": bits, "gated": isGated(name), "status": status,
                "message": check["message"], "downloaded": isDownloaded(name)}

    # The price of an API model for the picker: per 1 million tokens, and whether it fits in what is left of the budget (money, None without one).
    def apiEntry(self, name, provider, money, cli=None):
        price = self.costs.priceOf(getModelInfo(name, provider=provider, cli=cli))
        reference = price["reference"]
        status = "unknown" if reference is None else "overBudget" if money is not None and reference > money else "fits"
        return {"name": name, "provider": provider, "company": API_KEYS[provider]["company"], "price": reference, "priceText": price.get("error", ""), "status": status}

    # The first list of models only shows what can be chosen now: the local models that fit in the VRAM the other agents left, and the API models
    # whose price of 1 million tokens fits in what is left of the budget. All the models stay in the full lists, where the others are marked.
    def modelCatalog(self, payload):
        spec = self.findSpec(payload.get("agentId"))
        bits = int(payload.get("bits") or 16)
        if bits not in BITS:
            raise ValueError(f"The models are used with {', '.join(str(option) for option in BITS)} bits.")
        sizing = self.liveSwarm() if spec.get("pending") else self.sizingSwarm(spec["id"])
        recommend = getTask(spec["task"])["recommend"]
        families = {family: [self.localEntry(sizing, name, bits) for name in models] for family, models in MODELS_LOCAL.items()}
        local = [self.localEntry(sizing, name, bits) for name in RECOMMENDED_LOCAL[recommend]]
        budget = self.costs.left(self.describeTeam(excluding=spec["name"]), wait=True)
        money = budget["left"]
        try:
            prices = {provider: [self.apiEntry(name, provider, money) for name in models] for provider, models in MODELS_API.items()}
            claude = [self.apiEntry(name, "claude", money, cli="claude-code") for name in MODELS_CLI["claude-code"]["models"] if name != DEFAULT_CLI_MODEL]
        except FETCH_ERRORS as error:
            raise ValueError(f"The prices of the models could not be fetched: {describeError(error)}.") from None
        listed = {entry["name"]: entry for entries in prices.values() for entry in entries}
        api = [listed[name] for name in RECOMMENDED_API[recommend]]
        gpu = checkVram(sizing.getNeededVram(), readGpus())
        cli = {"claude-code": {"missing": findMissingPackages(getModelInfo("", cli="claude-code")), "problem": "", "prices": claude}, "codex": checkCodex()}
        return {"catalog": {"agentId": spec["id"], "bits": bits, "local": [entry for entry in local if entry["status"] != "tooBig"],
                            "hiddenLocal": sum(entry["status"] == "tooBig" for entry in local), "families": families,
                            "api": [entry for entry in api if entry["status"] != "overBudget"], "hiddenApi": sum(entry["status"] == "overBudget" for entry in api),
                            "prices": prices, "budget": budget, "gpu": gpu, "isLeader": self.specs[0] is spec, "hfToken": bool(os.environ.get("HF_TOKEN")),
                            "current": describeModel(spec["model"]), "cli": cli}}

    def lookupModel(self, payload):
        name = str(payload.get("name", "")).strip()
        if name.count("/") != 1 or " " in name:
            raise FormError({"name": "It is written owner/name, like Qwen/Qwen3-8B."}, "It is written owner/name, like Qwen/Qwen3-8B.")
        listed = next((family[name] for family in MODELS_LOCAL.values() if name in family), None)
        found = {"billions": listed, "gated": isGated(name)} if listed else lookupHuggingFace(name)
        return {"model": {"name": name, "billions": found["billions"], "gated": found["gated"] or isGated(name)}}

    def price(self, payload):
        name = str(payload.get("name", "")).strip()
        if payload.get("cli") == "codex":
            raise ValueError("Codex uses your ChatGPT plan: there is no price per use, only the limits of your plan.")
        info = getModelInfo(name, provider=payload.get("provider"), cli=payload.get("cli"))
        if info["local"]:
            raise ValueError("Local models are free to use: they run on your GPUs.")
        return {"price": describePrice(getModelCost(info["provider"], name))}

    def chooseModel(self, payload):
        spec = self.findSpec(payload.get("agentId"))
        self.checkIdle(spec)
        name = str(payload.get("name", "")).strip()
        if payload.get("cli"):
            info = getModelInfo(name, cli=payload["cli"])
            if info["cli"] == "codex":
                self.checkCodexReady()
        elif payload.get("local"):
            billions = float(payload["billions"]) if payload.get("billions") not in (None, "") else None
            if billions is not None and billions <= 0:
                raise FormError({"billions": "Write a number above 0, like 8.2."})
            info = getModelInfo(name, bits=int(payload.get("bits") or 16), billions=billions)
        else:
            info = getModelInfo(name, provider=payload.get("provider"))
            if info["local"]:
                raise ValueError(f"{name} is a model of Hugging Face: choose it in the local models.")
        # An agent about to join is checked against the swarm as it is (the agents that left freed their memory).
        check = (self.liveSwarm() if spec.get("pending") else self.sizingSwarm(spec["id"])).checkModel(info)
        if not check["allowed"]:
            raise ValueError(check["message"])
        if info["local"]:
            token = str(payload.get("hfToken") or "").strip()
            if token:
                self.tokens[name] = token
        elif info["provider"]:
            key = str(payload.get("apiKey") or "").strip()
            if key:
                self.keys[info["provider"]] = key
            if not (self.keys.get(info["provider"]) or getApiKey(info["provider"])):
                company = API_KEYS[info["provider"]]["company"]
                raise FormError({"apiKey": f"{company} needs an API key. Create one at {API_KEYS[info['provider']]['page']}."}, f"{company} needs an API key.")
        money = self.costs.left(self.describeTeam(excluding=spec["name"]), wait=True)["left"]
        price = self.costs.priceOf(info)["reference"] if not info["local"] else 0.0
        over = f"{info['name']} costs {formatDollars(price)} per 1 million tokens, more than the {formatDollars(max(money, 0))} left of the budget of the mission." \
            if money is not None and price is not None and price > money else ""
        client = self.makeClient(info, spec["name"])
        if hasattr(spec["client"], "unload"):
            spec["client"].unload()
        spec.update(model=info, client=client, missing=findMissingPackages(info), tested="")
        self.dirty = self.dirty or not spec.get("pending")
        return {"warning": check["message"] if info["local"] else over, "missing": spec["missing"], "download": describeDownload(info) if info["local"] else None}

    # ---------- Agents that join or leave the swarm while it runs (or between two runs, keeping what was approved). ----------
    # A new agent joins the group that works now (see Swarm.addAgent): it can wait for agents already there, and nobody waits for it.
    def canJoin(self, swarm):
        stage = swarm.stage
        return bool(stage and stage["open"] and (stage["plan"] or swarm.getLeader() not in stage["names"]))

    def joinLive(self, payload):
        spec = self.findSpec(payload.get("agentId"))
        swarm = self.liveSwarm()
        if not spec.get("pending"):
            raise ValueError(f"{spec['name']} is already in the swarm.")
        if swarm is None:
            raise ValueError("There is no swarm to join: add the agent in the steps instead.")
        if not spec["model"]:
            raise FormError({"model": f"Choose a model for {spec['name']} first."}, f"Choose a model for {spec['name']} first.")
        if payload.get("folder") is not None:
            self.setFolder({"agentId": spec["id"], "folder": payload.get("folder")})
        waits = [name for name in payload.get("waitsFor") or [] if name in swarm.getAgents() and name != swarm.getLeader()]
        loop = buildLoop(spec["task"], spec["client"], spec["answers"])
        self.connect(loop, spec["name"])
        recipe = {"task": spec["task"], "answers": publicAnswers(spec["task"], spec["answers"])}
        swarm.addAgent(spec["name"], loop, TASKS[spec["task"]]["role"], describeLoop(loop), waitsFor=waits, model=spec["model"], recipe=recipe)
        spec.update(pending=False, waitsFor=waits, description=describeLoop(loop))

    # The agent leaves at once (see Swarm.removeAgent); its model frees its memory as soon as its last step ended.
    def removeLive(self, payload):
        swarm = self.liveSwarm()
        if swarm is None:
            raise ValueError("There is no swarm: remove the agent in the steps instead.")
        name = str(payload.get("agent") or "")
        reason = str(payload.get("reason") or "").strip() or "The user removed it."
        swarm.removeAgent(name, reason)
        self.dropSpec(name)

    def testModel(self, payload):
        spec = self.findSpec(payload.get("agentId"))
        if not spec["client"]:
            raise ValueError("Choose a model first.")
        if spec["model"]["local"]:
            raise ValueError("A local model is tested when the swarm starts: it is loaded on the GPUs then.")
        if spec["model"].get("cli") == "codex":
            account = self.checkCodexReady()
            return {"problem": "", "answer": f"Codex is signed in{' as ' + account['email'] if account.get('email') else ''}."}
        try:
            answer = spec["client"].input(TEST_PROMPT)
        except ModelError as error:
            spec["tested"] = "failed"
            return {"problem": str(error)}
        spec["tested"] = "ok"
        return {"problem": "", "answer": " ".join(str(answer).split())[:80]}

    # ---------- Codex: signed in with the ChatGPT plan of the user, through Codex itself. ----------
    # The sign-in runs in its own thread: the page (or the code) is shown to the user, and the state says when Codex received the answer.
    def checkCodexReady(self):
        status = checkCodex()
        if status["problem"]:
            raise ValueError(status["problem"])
        account = readCodexAccount()
        if not account["signedIn"]:
            raise FormError({"codex": "Sign in to Codex with your ChatGPT account first."}, "Sign in to Codex with your ChatGPT account first.")
        return account

    def codexAccount(self, payload):
        status = checkCodex()
        if status["problem"]:
            return {"codex": {**status, "account": None, "models": []}}
        account = readCodexAccount()
        models = listCodexModels() if account["signedIn"] else []
        return {"codex": {**status, "account": account, "models": models}}

    def codexSignIn(self, payload):
        method = "code" if payload.get("method") == "code" else "browser"
        with self.lock:
            if self.codexLogin and self.codexLogin["state"] == "waiting":
                self.codexLogin["login"].cancel()
        login = CodexLogin()
        try:
            target = login.start(method)
        except ModelError:
            login.cancel()
            raise
        state = {"state": "waiting", "method": method, "url": target["url"], "code": target["code"], "error": "", "account": None, "login": login}
        with self.lock:
            self.codexLogin = state
        if method == "browser":
            webbrowser.open(target["url"])
        def wait():
            error = login.wait()
            account = None
            if not error:
                try:
                    account = readCodexAccount()
                except ModelError as problem:
                    error = str(problem)
            with self.lock:
                if self.codexLogin is state:
                    state.update(state="failed" if error else "done", error=error, account=account)
                self.touch()
        threading.Thread(target=wait, daemon=True).start()

    def codexCancel(self, payload):
        with self.lock:
            state, self.codexLogin = self.codexLogin, None
        if state and state["state"] == "waiting":
            state["login"].cancel()

    def checkPackages(self, payload):
        for spec in self.specs:
            if spec["model"]:
                spec["missing"] = findMissingPackages(spec["model"])
