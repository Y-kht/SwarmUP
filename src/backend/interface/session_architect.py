# The architect in the window (Session in session_core.py): the model the user chooses for it in the step of the models (the same as an agent
# of the swarm, the leader by default, or a model of its own), what it does while the swarm runs, the wheels of each agent, and the page of what
# the architect learned: the patterns of each project, the wheels with their approvals, and the notes (architect_admin.py, as the command line).
from architect_admin import approveWheel, architectNotes, describeProjects, hasPatterns, mine, patternBrief, patternCounts, patternDetail, patternRows, retireByUser, revokeWheel, searchJournals, wheelDetail, wheelRows
from architect_loop import pickArchitect
from architect_manager import attachArchitect
from harness_utils import ARCHITECT_NAME
from interface_views import describeModel
from leader_wheels import checkWheels, giveWheels, projectOfFolder
from pattern_store import ACTIONABLE
from wheel_store import catalogFor


# The architect has a spec like the agents, so the picker of the models works for it too.
ARCHITECT_ID = "architect"
PATTERNS_SHOWN = 100
PATTERN_KINDS = list(ACTIONABLE)
PATTERN_STATUSES = ["new", "emerging", "repeating", "cross", "covered", "noted", "dismissed"]


class SessionArchitect:
    def resetArchitect(self):
        self.architectSpec = {"id": ARCHITECT_ID, "task": "architect", "name": ARCHITECT_NAME, "answers": {}, "model": None, "client": None, "missing": [], "waitsFor": [],
                              "pending": False, "tested": "", "same": None, "description": ""}

    # The agents whose model the architect can share: not the user, not Codex.
    def architectOptions(self):
        return [spec["name"] for spec in self.specs if spec["model"] and not spec["model"].get("user") and spec["model"].get("cli") != "codex"]

    def architectChoice(self):
        spec = self.architectSpec
        if spec["model"]:
            return spec["model"]
        options = self.architectOptions()
        same = spec["same"] if spec["same"] in options else options[0] if options else None
        return {"same": same} if same else None

    def setArchitect(self, payload):
        self.checkIdle()
        same = payload.get("same")
        if same not in self.architectOptions():
            raise ValueError("The architect can use the model of an agent that has one (not you, and not Codex), or a model of its own.")
        spec = self.architectSpec
        if hasattr(spec["client"], "unload"):
            spec["client"].unload()
        spec.update(model=None, client=None, same=same, tested="", missing=[])
        self.dirty = True

    def describeArchitect(self):
        choice = self.architectChoice()
        return {"id": ARCHITECT_ID, "same": (choice or {}).get("same"), "model": describeModel(self.architectSpec["model"]), "options": self.architectOptions(),
                "missing": self.architectSpec["missing"], "wheels": self.wheels({})["wheels"] if self.specs else []}

    def describeArchitectRun(self):
        swarm = self.swarm
        return swarm.architect.describe() if swarm is not None and swarm.architect is not None else None

    # Every swarm of the window gets its architect, on the model the user chose (or, for a saved swarm, the one it had).
    def attachTheArchitect(self, swarm, choice=None, saved=None):
        spec = self.architectSpec

        def makeClient(info):
            return spec["client"] if spec["client"] is not None and info is spec["model"] else self.makeClient(info, ARCHITECT_NAME)
        architect = attachArchitect(swarm, choice if choice is not None else self.architectChoice(), makeClient, lambda text: self.notify(ARCHITECT_NAME, text))
        if saved is not None:
            architect.restoreState(saved)
        return architect

    def closeTheArchitect(self):
        swarm = self.swarm
        if swarm is not None and swarm.architect is not None:
            swarm.architect.close()

    def architectModelName(self):
        info = pickArchitect(self.swarm, self.architectChoice())[0] if self.swarm is not None else None
        return (info or {}).get("name") or ""

    # ---------- The wheels of the agents. ----------
    def projectOfSpecs(self):
        folders = [spec["answers"].get("folder") for spec in self.specs if spec["answers"].get("folder")]
        return self.swarm.project if self.swarm is not None and self.swarm.project else projectOfFolder(max(folders, key=folders.count) if folders else None)

    def wheels(self, payload):
        catalog = catalogFor(self.projectOfSpecs())
        return {"wheels": [{"name": name, "summary": entry.get("summary") or "", "version": entry["version"], "kind": entry["kind"], "scope": entry.get("scope")}
                           for name, entry in catalog.items()]}

    def setWheels(self, payload):
        spec = self.findSpec(payload.get("agentId"))
        if spec["id"] == ARCHITECT_ID:
            raise ValueError("The architect writes the wheels; it does not use them.")
        names, errors = checkWheels(payload.get("wheels") or [], self.projectOfSpecs())
        if errors:
            raise ValueError(" ".join(errors))
        swarm = self.swarm
        if swarm is not None and swarm.isRunning() and spec["name"] in swarm.members:
            giveWheels(swarm, spec["name"], names, "given by the user")
        else:
            self.dirty = True
        spec["answers"]["wheels"] = names
        return {"wheels": names}

    # ---------- What the architect learned, project by project. ----------
    # The projects SwarmUP knows, the one of this mission first, and the wheels of the chosen project.
    def learning(self, payload):
        projects, current = describeProjects(), self.projectOfSpecs()
        projects.sort(key=lambda project: project["id"] != current)
        known = {project["id"] for project in projects}
        project = payload.get("project") if payload.get("project") in known else projects[0]["id"] if projects else None
        return {"projects": projects, "project": project, "counts": patternCounts(project) if project and hasPatterns(project) else None, "wheels": wheelRows(project),
                "kinds": PATTERN_KINDS, "statuses": PATTERN_STATUSES}

    def patterns(self, payload):
        kinds, statuses = [payload["kind"]] if payload.get("kind") else [], [payload["status"]] if payload.get("status") else []
        return {"patterns": patternRows(payload.get("project"), kinds, statuses, PATTERNS_SHOWN)}

    def patternBrief(self, payload):
        return {"text": patternBrief(payload.get("project"))}

    def patternDetail(self, payload):
        return {"text": patternDetail(payload.get("project"), payload.get("id"))}

    def searchPatterns(self, payload):
        return {"text": searchJournals(payload.get("project"), str(payload.get("text") or ""))}

    def minePatterns(self, payload):
        return {"text": mine(payload.get("project"), bool(payload.get("rebuild")))}

    def wheelDetail(self, payload):
        return {"wheel": wheelDetail(str(payload.get("name") or ""))}

    def approveWheel(self, payload):
        return {"text": approveWheel(str(payload.get("name") or ""), "in the window")}

    def revokeWheel(self, payload):
        return {"text": revokeWheel(str(payload.get("name") or ""))}

    def retireWheel(self, payload):
        return {"text": retireByUser(str(payload.get("name") or ""), payload.get("why"))}

    def architectNotes(self, payload):
        return {"notes": architectNotes(str(payload.get("query") or ""))}
