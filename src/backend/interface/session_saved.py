import os
import threading
import webbrowser

from base_loop import Loop
from interface_views import FormError, describeField, describeModel, hasCredentials
from model_clients import createModel
from model_support import findMissingPackages
from models_library import API_KEYS, isGated
from harness_utils import USER_NAME
from mission_history import loadMission
from saved_swarms import UNFINISHED_STATES, findUnfinishedSwarms
from swarm_harness import Swarm
from tasks_library import buildLoop, describeLoop, restoreAnswers, secretFields


SHUTDOWN_DELAY = 0.5


# The missions of the history (Session in session_core.py): the swarms that were interrupted, continued where they stopped, and the missions whose
# round is over, opened again to follow them up. Also the pages of the providers, and the end of the program.
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
        try:
            return loadMission(swarmId)
        except ValueError as error:
            raise ValueError(f"{error} If its window was closed a few seconds ago, try again in half a minute." if "running" in str(error) else str(error)) from None

    # A mission whose round is over is followed up: every agent may work again, so it needs its secrets again.
    def isFollowUp(self, saved):
        return saved["state"] not in UNFINISHED_STATES

    # What the user must give again to continue a swarm: the secrets of every agent (never saved), the API keys and the Hugging Face tokens.
    def resumeForm(self, payload):
        saved = self.findSaved(payload.get("id"))
        if not all(member.get("recipe") for member in saved["members"].values()):
            raise ValueError("This swarm was made by another program, so it cannot be continued here.")
        agents, providers, gated, codex = [], {}, [], False
        following = self.isFollowUp(saved)
        for name, member in saved["members"].items():
            task, answers, finished = member["recipe"]["task"], member["recipe"]["answers"], member["status"] in ("done", "failed") and not following
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
                           "codex": codex, "followUp": following, "round": saved.get("round", 1), "requests": saved.get("requests") or []}}

    def rebuildAgent(self, name, data, secretValues, rebuilt, following=False):
        recipe, finished = data["recipe"], data["status"] in ("done", "failed") and not following
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
        rebuilt, following = [], self.isFollowUp(saved)
        swarm = Swarm.restore(saved, lambda name, data: self.rebuildAgent(name, data, payload.get("secrets") or {}, rebuilt, following))
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
        if following:
            self.showTranscript(saved)
            with self.lock:
                self.runInfo = {"startedAt": saved.get("savedAt"), "finishedAt": saved.get("savedAt"), "resume": False, "reopened": True}
        else:
            self.launch(swarm, resume=True)
        self.refreshUnfinished()

    # What a mission that is opened again already said: the requests of the user and the messages of its rounds, as they were in the conversation.
    def showTranscript(self, saved):
        requests = saved.get("requests") or [{"round": 1, "text": saved["mission"]}]
        self.addFeed("", f"The mission is open again. It had {len(requests)} {'round' if len(requests) == 1 else 'rounds'}: what was said is below.", "info", "event")
        for request in requests:
            self.addFeed(USER_NAME, f"Round {request['round']}: {request['text']}", "info", "answer")
        for message in saved.get("messages") or []:
            self.addFeed(message["sender"], message["message"], "info", "message" if message["sender"] != USER_NAME else "answer", message["receiver"])
        report = saved["members"][saved["leader"]].get("result")
        if report:
            self.addFeed(saved["leader"], f"My last report:\n{report}", "info", "note")

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
