from pathlib import Path

from harness_utils import ConnectionLost, FETCH_ERRORS, describeError
from interface_views import FormError, browseFolder, describeField, describeRawValue, openInFileManager
from message_loops import checkEmailLogin
from messengers import MessagingError, checkMessenger, findTelegramChats
from sources_library import MESSAGING_APPS
from tasks_library import (ADVANCED_FIELDS, TASKS, answerKey, buildLoop, checkAgentName, describeLoop, getDefault, getFields, isAsked, messengerSettings, parseAnswer,
                           readAnswers, suggestFolder, suggestName)
from writing_loops import findPublishers


# The first steps of the window (Session in session_core.py): the mission, the agents with the forms of their tasks, and their folders.
class SessionSteps:
    # ---------- Step 1: the mission and the agents. ----------
    def setMission(self, payload):
        mission = str(payload.get("mission", "")).strip()
        if not mission:
            raise FormError({"mission": "Write the mission of the swarm in a sentence or two."})
        if mission != self.mission:
            self.mission, self.dirty = mission, True
            leader = self.leaderSpec()
            if leader:
                leader["answers"]["mission"] = mission

    # The answers so far, as they are typed, to know which questions are asked and what their defaults and help are.
    def roughAnswers(self, task, values):
        answers = {}
        for field in getFields(task):
            if isAsked(field, answers):
                raw = values.get(field["key"])
                answers[field["key"]] = getDefault(field, answers) if raw in (None, "") else raw
        return answers

    def rawValues(self, spec):
        return {field["key"]: describeRawValue(field, spec["answers"].get(field["key"])) for field in getFields(spec["task"]) if field["key"] in spec["answers"]}

    def taskForm(self, payload):
        task = payload.get("task")
        if task == "leader":
            raise ValueError("The leader has no questions: it builds the swarm from your mission. Change the mission, its folder or its model instead.")
        if task not in TASKS:
            raise ValueError("Choose one of the tasks.")
        spec = self.findSpec(payload["agentId"]) if payload.get("agentId") else None
        previous = spec["answers"] if spec and spec["task"] == task else None
        values = payload.get("values")
        if values is None:
            values = self.rawValues(spec) if previous else {}
        answers = self.roughAnswers(task, values)
        fields = [{**describeField(field, answers, previous), "visible": isAsked(field, answers)} for field in TASKS[task]["fields"]]
        taken = [other["name"] for other in self.specs if other is not spec]
        return {"form": {"task": task, "label": TASKS[task]["label"], "info": TASKS[task]["info"], "fields": fields, "values": values,
                         "advanced": [describeField(field, answers) for field in ADVANCED_FIELDS], "name": spec["name"] if spec else suggestName(task, taken),
                         "agentId": spec["id"] if spec else None}}

    # The Telegram chat is found from the messages the user sent to the bot, when the user did not give it.
    def findTelegramChat(self, answers, errors):
        chat = answerKey("Telegram", "chat")
        if answers.get("messenger") != "Telegram" or answers.get(chat) or errors:
            return
        try:
            chats = findTelegramChats(answers[answerKey("Telegram", "token")])
        except (MessagingError, ConnectionLost) as error:
            errors[chat] = str(error)
            return
        if len(chats) == 1:
            answers[chat] = str(chats[0]["id"])
        else:
            errors[chat] = ("Open your bot in Telegram, press Start and send it any message, then press Find my chat." if not chats else
                            "Several chats wrote to your bot: press Find my chat and choose yours.")

    # live (from the live view) prepares an agent that joins the swarm afterwards (joinLive): until then it is pending, and the swarm does not change.
    def saveAgent(self, payload):
        task = payload.get("task")
        if task not in TASKS:
            raise ValueError("Choose one of the tasks.")
        spec = self.findSpec(payload["agentId"]) if payload.get("agentId") else None
        live = (spec.get("pending") if spec else bool(payload.get("live"))) and self.liveSwarm() is not None
        if not live:
            self.checkIdle()
        previous = spec["answers"] if spec and spec["task"] == task else None
        answers, errors = readAnswers(task, payload.get("values") or {}, previous)
        taken = [other["name"] for other in self.specs if other is not spec]
        name = str(payload.get("name") or "").strip() or suggestName(task, taken)
        if checkAgentName(name, taken):
            errors["name"] = checkAgentName(name, taken)
        self.findTelegramChat(answers, errors)
        if errors:
            raise FormError(errors)
        folder, warning = (spec["answers"].get("folder") if spec else None), ""
        try:
            loop = buildLoop(task, None, {**answers, "folder": folder})
        except ValueError as problem:
            if not folder:
                raise FormError({}, str(problem))
            folder, warning = None, f"The folder of this agent was removed: {problem}"
            loop = buildLoop(task, None, {**answers, "folder": None})
        answers["folder"] = folder
        if spec is None:
            self.agentNumber += 1
            spec = {"id": f"agent{self.agentNumber}", "model": None, "client": None, "waitsFor": [], "missing": [], "pending": live}
            self.specs.append(spec)
        elif spec["name"] != name:
            for other in self.specs:
                other["waitsFor"] = [name if waited == spec["name"] else waited for waited in other["waitsFor"]]
            self.costs.rename(spec["name"], name)
        spec.update(task=task, name=name, answers=answers, description=describeLoop(loop))
        self.dirty = self.dirty or not live
        return {"agentId": spec["id"], "description": spec["description"], "warning": warning}

    def removeAgent(self, payload):
        spec = self.findSpec(payload.get("agentId"))
        if spec["task"] == "leader":
            raise ValueError("This leader builds your swarm. To build it yourself instead, choose it in the step of the mission.")
        if spec.get("pending"):
            self.specs.remove(spec)
            if hasattr(spec["client"], "unload"):
                spec["client"].unload()
            return
        self.checkIdle()
        index = self.specs.index(spec)
        self.specs.remove(spec)
        for other in self.specs:
            other["waitsFor"] = [name for name in other["waitsFor"] if name != spec["name"]]
        self.trash = {"spec": spec, "index": index}
        self.dirty = True
        self.sanitizeWaits()

    def undoRemove(self, payload):
        self.checkIdle()
        if not self.trash:
            raise ValueError("There is nothing to bring back.")
        spec, index = self.trash["spec"], self.trash["index"]
        if checkAgentName(spec["name"], [other["name"] for other in self.specs]):
            raise ValueError(f"Another agent is called {spec['name']} now, so it cannot come back.")
        self.specs.insert(min(index, len(self.specs)), spec)
        self.trash, self.dirty = None, True
        self.sanitizeWaits()

    # The first agent is the leader. It works last, so nobody can wait for it, and it waits for nobody.
    def sanitizeWaits(self):
        if not self.specs:
            return
        leader = self.specs[0]["name"]
        names = [spec["name"] for spec in self.specs]
        self.specs[0]["waitsFor"] = []
        for spec in self.specs[1:]:
            spec["waitsFor"] = [name for name in spec["waitsFor"] if name in names and name not in (leader, spec["name"])]

    def moveAgent(self, payload):
        self.checkIdle()
        spec = self.findSpec(payload.get("agentId"))
        position = max(0, min(int(payload.get("position", 0)), len(self.specs) - 1))
        if self.leaderSpec() and (spec["task"] == "leader" or position == 0):
            raise ValueError("The leader that builds the swarm stays first: it leads it.")
        self.specs.remove(spec)
        self.specs.insert(position, spec)
        self.dirty = True
        self.sanitizeWaits()

    def checkEmail(self, payload):
        spec = self.findSpec(payload["agentId"]) if payload.get("agentId") else None
        answers, errors = readAnswers("email", payload.get("values") or {}, spec["answers"] if spec and spec["task"] == "email" else None)
        needed = {key: error for key, error in errors.items() if key in ("provider", "sender", "password", "smtp", "imap")}
        if needed:
            raise FormError(needed, "Fill in the account first.")
        return {"problem": checkEmailLogin(answers["sender"], answers["password"], answers["smtp"], answers["imap"])}

    def messengerAnswers(self, payload):
        spec = self.findSpec(payload["agentId"]) if payload.get("agentId") else None
        answers, errors = readAnswers("news", payload.get("values") or {}, spec["answers"] if spec and spec["task"] == "news" else None)
        app = answers.get("messenger")
        if app not in MESSAGING_APPS:
            raise ValueError("Choose a messaging app first.")
        return app, answers, errors

    def checkMessenger(self, payload):
        app, answers, errors = self.messengerAnswers(payload)
        needed = {key: error for key, error in errors.items() if key.startswith(app.lower())}
        if needed:
            raise FormError(needed, f"Fill in what {app} needs first.")
        return {"problem": checkMessenger(app, messengerSettings(answers))}

    def findChats(self, payload):
        app, answers, errors = self.messengerAnswers(payload)
        token = answerKey("Telegram", "token")
        if token in errors:
            raise FormError({token: errors[token]}, "Give the token of your bot first.")
        return {"chats": [{"id": str(chat["id"]), "name": chat["name"]} for chat in findTelegramChats(answers[token])]}

    def searchPublishers(self, payload):
        name = str(payload.get("name", "")).strip()
        if not name:
            raise ValueError("Write the name of the publisher.")
        try:
            found = findPublishers(name)
        except FETCH_ERRORS as error:
            raise ValueError(f"Crossref could not be asked: {describeError(error)}.") from None
        return {"publishers": [{"name": publisher["name"], "id": str(publisher["id"]), "papers": publisher["papers"]} for publisher in found]}

    def browse(self, payload):
        return {"browse": browseFolder(str(payload.get("path") or ""), bool(payload.get("files")), bool(payload.get("hidden")))}

    # The dialog of the system, when the window has one. The browser shows its own browser of folders otherwise.
    def pickPath(self, payload):
        kind = payload.get("kind")
        if not self.dialog or kind not in ("folder", "file", "path"):
            raise ValueError("The dialogs of the system are not available in this window.")
        return {"path": self.dialog(kind, str(payload.get("path") or ""))}

    # ---------- Step 2: the folder of each agent. ----------
    def setFolder(self, payload):
        spec = self.findSpec(payload.get("agentId"))
        self.checkIdle(spec)
        text, folder = str(payload.get("folder") or "").strip(), None
        if text:
            folder, error = parseAnswer({"key": "folder", "ask": "", "kind": "folder"}, text)
            if error:
                raise FormError({"folder": error}, error)
        try:
            loop = buildLoop(spec["task"], None, {**spec["answers"], "folder": folder})
        except ValueError as problem:
            raise FormError({"folder": str(problem)}, str(problem)) from None
        spec["answers"]["folder"], spec["description"] = folder, describeLoop(loop)
        self.dirty = self.dirty or not spec.get("pending")

    def openFolder(self, payload):
        path = str(payload.get("path") or "")
        known = {str(spec["answers"].get("folder")) for spec in self.specs if spec["answers"].get("folder")}
        known |= {suggestFolder(spec["answers"]) for spec in self.specs if suggestFolder(spec["answers"])}
        if path not in known or not Path(path).is_dir():
            raise ValueError("Only the folders of the agents can be opened.")
        openInFileManager(path)
