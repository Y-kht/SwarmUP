# The memory of the GPUs of a swarm (Swarm in swarm_harness.py). While it is built, a swarm is expected to need the sum of its local models, so they
# all fit whatever runs together (getNeededVram, checkModel, checkGpus). While it runs, the user sees what the GPUs hold now (getLiveVram): a model only
# holds memory while its agent can work. In plan mode all the agents plan together, so all of them count. In execute mode an agent that waits
# for another one, that sits a round out or that is done has let its memory go, so only the ones at work count, and the leader, which writes the summaries.
import traceback

from gpu_check import checkVram, programMemory, readGpus
from harness_utils import ARCHITECT_NAME


class SwarmVram:
    # The VRAM in GB that the models of the agents need together, without the agent called replacing.
    def getNeededVram(self, replacing=None):
        return round(sum(member["model"]["vram"] for name, member in self.members.items() if member["model"] and name != replacing), 1)

    # The VRAM in GB that the models of the agents hold now: the leader that built the swarm, the agents after their plan or the round before.
    def getLoadedVram(self):
        return round(sum(member["model"]["vram"] for member in self.members.values() if member["model"] and getattr(getattr(member["agent"], "agent", None), "isLoaded", False)), 1)

    # What the user sees before choosing a model for an agent, or to replace the model of the agent called replacing.
    # A local model that does not fit in the GPUs is not allowed, and the message says why. One that fits, but not in the memory
    # that is free now, is allowed and the message is a warning. API models are always allowed, but they do not hide the warning of the swarm.
    def checkModel(self, model, replacing=None):
        taken = self.getNeededVram(replacing)
        after = round(taken + model["vram"], 1)
        status = checkVram(after, readGpus() if after else [], self.getLoadedVram())
        name, vram, total = model["name"], model["vram"], status["total"]
        if model["local"] and status["gpus"] and vram > total:
            message = f"{name} needs about {vram} GB of VRAM, but your GPUs only have {total} GB in total. Choose a smaller model or an API model."
        elif model["local"] and status["gpus"] and not status["fits"]:
            message = (f"{name} needs about {vram} GB of VRAM. The other agents already take {taken} GB and your GPUs have {total} GB in total, "
                       "so there is no room left. Choose a smaller model or an API model.")
        else:
            message = status["message"]
        return {"allowed": status["fits"] or not model["local"], "vram": vram, "needed": after, "message": message}

    # The VRAM the swarm needs so far next to what the GPUs have, for the user interface to show while the swarm is built.
    # The message is empty, or the warning that the swarm cannot run now.
    def getVramStatus(self):
        return checkVram(self.getNeededVram(), readGpus(), self.getLoadedVram())

    def checkGpus(self):
        needed = self.getNeededVram()
        status = checkVram(needed, readGpus() if needed else [], self.getLoadedVram())
        if not status["runnable"]:
            raise ValueError(status["message"])

    # A model of the agents lets its memory go. It loads again if it is needed later.
    def letGo(self, member):
        model = member["agent"].agent
        if getattr(model, "local", False) is True and hasattr(model, "unload"):
            try:
                model.unload()
            except Exception:
                traceback.print_exc()

    # An agent that does not work now in execute mode needs its model no more for a while: its memory goes to the agents at work.
    def release(self, member):
        if member["mode"] == "execute":
            self.letGo(member)

    # The memory a client holds now: {"gb", "gpus": their numbers for PyTorch}, or None. A client that cannot tell holds what it was expected to need.
    def heldBy(self, client, info):
        if not getattr(client, "isLoaded", False):
            return None
        return getattr(client, "held", None) or {"gb": (info or {}).get("vram") or 0.0, "gpus": []}

    def getHeld(self, name):
        member = self.anyMember(name)
        return self.heldBy(member["agent"].agent, member["model"])

    # Who holds memory now: the agents, those that just left and whose model did not let go yet, and an architect with a model of its own.
    def describeHolders(self):
        holders = []
        for name, member in [*list(self.members.items()), *list(self.departed.items())]:
            held = self.heldBy(getattr(member["agent"], "agent", None), member["model"])
            if held:
                holders.append({"name": name, "status": member["status"], **held})
        architect = self.architect
        if architect is not None and architect.how == "own":
            held = self.heldBy(architect.client, architect.info)
            if held:
                holders.append({"name": ARCHITECT_NAME, "status": architect.state, **held})
        return holders

    # What the GPUs hold now, for the user. Each GPU as its driver reads it, with what SwarmUP holds on it, what other jobs use, and the agents on it;
    # then the same in total. needed is what the swarm was expected to need when it was built.
    def getLiveVram(self):
        gpus, holders, rows = readGpus(), self.describeHolders(), []
        program = programMemory(gpus)
        numbers = {number: gpu.get("index", number) for number, gpu in enumerate(gpus)}
        for holder in holders:
            holder["gpus"] = [numbers.get(number, number) for number in holder["gpus"]]
        for number, gpu in enumerate(gpus):
            used = round(gpu["total"] - gpu["free"], 1)
            mine = min(program[number], used)
            rows.append({"index": numbers[number], "name": gpu["name"], "total": gpu["total"], "free": gpu["free"], "used": used, "program": mine, "others": round(used - mine, 1),
                         "agents": [holder["name"] for holder in holders if numbers[number] in holder["gpus"]]})
        return {"gpus": rows, "agents": holders, "needed": self.getNeededVram(), "total": round(sum(row["total"] for row in rows), 1), "free": round(sum(row["free"] for row in rows), 1),
                "used": round(sum(row["used"] for row in rows), 1), "program": round(sum(row["program"] for row in rows), 1), "others": round(sum(row["others"] for row in rows), 1)}

    # The memory changes without any other event of the swarm (a model loads, another job ends): the user interfaces read it again when it changed.
    def noteVram(self):
        if not self.getNeededVram():
            return
        live = self.getLiveVram()
        seen = (live["used"], [(holder["name"], holder["gb"]) for holder in live["agents"]])
        if seen != self.vramSeen:
            self.vramSeen = seen
            self.emit("vram")
