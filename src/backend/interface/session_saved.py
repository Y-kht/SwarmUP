import os
import threading
import webbrowser

from base_loop import Loop
from interface_views import FormError, describeField, describeModel, hasCredentials
from model_clients import createModel
from model_support import findMissingPackages
from models_library import API_KEYS, isGated
from saved_swarms import findUnfinishedSwarms
from swarm_harness import Swarm
from tasks_library import buildLoop, describeLoop, restoreAnswers, secretFields


SHUTDOWN_DELAY = 0.5


# The swarms that were interrupted (Session in session_core.py), the pages of the providers, and the end of the program.
class SessionSaved:
    # ---------- The swarms that were interrupted. ----------
    def refreshUnfinished(self, payload=None):
        found = []
        for saved in findUnfinishedSwarms():
            members = [{"name": name, "role": member["role"], "status": member["status"], "task": (member.get("recipe") or {}).get("task")}
                       for name, member in saved["members"].items()]
            found.append({"id": saved["id"], "mission": saved["mission"], "savedAt": saved["savedAt"], "mode": saved["mode"], "running": saved["running"],
                          "reason": "The connection was lost" if saved["state"] == "paused" else "The program or the computer stopped",
                          "canResume": all(member.get("recipe") for member in saved["members"].values()), "members": members, "leader": saved["leader"]})
        self.unfinished = found

    def findSaved(self, swarmId):
        saved = next((saved for saved in findUnfinishedSwarms() if saved["id"] == swarmId), None)
        if saved is None:
            raise ValueError("This swarm is not saved anymore.")
        if saved["running"]:
            raise ValueError("This swarm is working in another window of the program. If that window was closed a few seconds ago, try again in half a minute.")
        return saved

    # What the user must give again to continue a swarm: the secrets of every agent (never saved), the API keys and the Hugging Face tokens.
    def resumeForm(self, payload):
        saved = self.findSaved(payload.get("id"))
        if not all(member.get("recipe") for member in saved["members"].values()):
            raise ValueError("This swarm was made by another program, so it cannot be continued here.")
        agents, providers, gated, codex = [], {}, [], False
        for name, member in saved["members"].items():
            task, answers, finished = member["recipe"]["task"], member["recipe"]["answers"], member["status"] in ("done", "failed")
            fields = [{**describeField(field, answers), "required": field.get("required", False) and not finished} for field in secretFields(task, answers)]
            info = member["model"]
            agents.append({"name": name, "role": member["role"], "task": task, "status": member["status"], "finished": finished, "fields": fields,
                           "accounts": task == "literature" and not finished, "model": describeModel(info), "missing": findMissingPackages(info) if info else []})
            if info and not info["local"] and info["provider"] and not finished:
                providers[info["provider"]] = self.describeKeys()[info["provider"]]
            codex = codex or bool(info and info.get("cli") == "codex" and not finished)
            if info and info["local"] and isGated(info["name"]) and not os.environ.get("HF_TOKEN") and not finished:
                gated.append(info["name"])
        return {"resume": {"id": saved["id"], "mission": saved["mission"], "mode": saved["mode"], "agents": agents, "providers": providers, "gated": gated,
                           "codex": codex}}

    def rebuildAgent(self, name, data, secretValues, rebuilt):
        recipe, finished = data["recipe"], data["status"] in ("done", "failed")
        task, values = recipe["task"], secretValues.get(name) or {}
        secrets, errors = {}, {}
        for field in secretFields(task, recipe["answers"]):
            text = str(values.get(field["key"]) or "")
            if not text and field.get("required") and not finished:
                errors[f"{name}.{field['key']}"] = "This is needed to continue."
            secrets[field["key"]] = text
        if task == "literature":
            secrets["accounts"] = {str(row.get("host", "")).strip(): (str(row.get("user", "")).strip(), str(row.get("password", ""))) for row in values.get("accounts") or []
                                   if str(row.get("host", "")).strip() and str(row.get("user", "")).strip()}
        info, client = data["model"], None
        usable = info and hasCredentials(info, self.keys)
        if info and not usable and not finished:
            errors[f"key.{info['provider']}"] = f"{API_KEYS[info['provider']]['company']} needs an API key."
        if errors:
            raise FormError(errors)
        if usable:
            client = self.makeClient(info, name)
        answers = restoreAnswers(task, recipe["answers"], secrets)
        loop = buildLoop(task, client, answers)
        self.connect(loop, name)
        self.agentNumber += 1
        rebuilt.append({"id": f"agent{self.agentNumber}", "task": task, "name": name, "answers": answers, "model": info, "client": client,
                        "waitsFor": list(data["waitsFor"]), "missing": findMissingPackages(info) if info else [], "description": describeLoop(loop)})
        return loop

    def rememberKeys(self, payload):
        self.keys.update({provider: str(key).strip() for provider, key in (payload.get("keys") or {}).items() if provider in API_KEYS and str(key).strip()})
        self.tokens.update({name: str(token).strip() for name, token in (payload.get("tokens") or {}).items() if str(token).strip()})

    def resume(self, payload):
        if self.isBusy():
            raise ValueError("Wait until the work in progress is finished.")
        saved = self.findSaved(payload.get("id"))
        self.rememberKeys(payload)
        if any((member.get("model") or {}).get("cli") == "codex" and member["status"] not in ("done", "failed") for member in saved["members"].values()):
            self.checkCodexReady()
        rebuilt = []
        swarm = Swarm.restore(saved, lambda name, data: self.rebuildAgent(name, data, payload.get("secrets") or {}, rebuilt))
        swarm.addListener(self.onEvent)
        self.unloadModels()
        self.reset()
        rebuilt.sort(key=lambda spec: spec["name"] != saved["leader"])
        self.mission, self.specs, self.mode, self.swarm, self.dirty, self.costs = saved["mission"], rebuilt, saved["mode"], swarm, False, swarm.costs
        if saved.get("managed") and self.leaderSpec():
            self.buildMode = "leader"
            self.manage(swarm, self.leaderSpec())
        self.order = "custom" if any(spec["waitsFor"] for spec in rebuilt) else "together"
        self.cancelling = None
        self.launch(swarm, resume=True)
        self.refreshUnfinished()

    # Before a saved swarm is cancelled, the leader says what it already changed. It writes that with its model if it can be made again.
    def prepareCancel(self, payload):
        saved = self.findSaved(payload.get("id"))
        self.rememberKeys(payload)
        def rebuild(name, data):
            recipe = data.get("recipe") or {}
            try:
                loop = buildLoop(recipe["task"], None, {**restoreAnswers(recipe["task"], recipe["answers"]), "folder": None})
            except (KeyError, ValueError, OSError):
                loop = Loop(None)
            self.connect(loop, name)
            return loop
        swarm = Swarm.restore(saved, rebuild)
        info = saved["members"][saved["leader"]]["model"]
        if info and not payload.get("plain") and hasCredentials(info, self.keys):
            swarm.getMember(swarm.getLeader())["agent"].agent = createModel(info, self.keys, token=self.tokens.get(info["name"]))
        def work():
            summary = swarm.summarizeChanges()
            with self.lock:
                self.cancelling = {"id": saved["id"], "mission": saved["mission"], "summary": summary, "swarm": swarm}
            return summary
        self.runJob("cancel", "The leader is listing what the swarm already did", work)

    def abandon(self, payload):
        if not self.cancelling or self.cancelling["id"] != payload.get("id"):
            raise ValueError("Read the summary of the leader before you stop this swarm.")
        self.cancelling["swarm"].abandon()
        self.cancelling = None
        self.jobs.pop("cancel", None)
        self.refreshUnfinished()

    # The pages of the providers (API keys, prices, licences) open in the web browser of the user, outside the window of SwarmUP.
    def openLink(self, payload):
        url = str(payload.get("url") or "")
        if not url.startswith(("https://", "http://")):
            raise ValueError("Only web addresses can be opened.")
        webbrowser.open(url)

    # ---------- Leaving the program. ----------
    def close(self):
        with self.lock:
            self.closing = True
            self.touch()
        if self.swarm:
            self.swarm.saveForExit()
        self.unloadModels()

    def quit(self, payload):
        threading.Timer(SHUTDOWN_DELAY, self.shutdown).start()
        return {"message": "SwarmUP is closing. Your swarm is saved: start the program again to continue it."}

    def shutdown(self):
        self.close()
        os._exit(0)
