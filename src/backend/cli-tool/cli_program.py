# The swarm of the command line: its agents, its order, its run, and the leader that builds it.
from cli_console import askUntilValid, askYesNo, chooseFrom, heading, shorten
from cli_models import chooseModel
from cli_steps import askFolder, chooseTask, fillTask
from cli_view import TreeView, describeCosts, renderTree, runCommand, watchCosts
from leader_catalog import LeaderCatalog
from leader_manager import LeaderManager
from leader_utils import designSwarm
from model_clients import createModel
from model_support import ModelError
from swarm_harness import Swarm
from tasks_library import DEFAULT_LOOPS, LEADER_TASK, buildLoop, describeLoop, getTask, parseAnswer, publicAnswers


# ==============
# The swarm: its agents, its order, and its run.
# ==============
# The leader tells the user to click on an agent, as a graphical interface does. In the command line the name is typed.
def forTerminal(message):
    return message.replace("by clicking on its name in the swarm", "by typing its name (type help to see how)")


# What an agent says and asks goes to its own window in a session (its name is read when it speaks, because the swarm names it).
def connect(loop, console):
    loop.notifyUser = lambda message: console.say(forTerminal(message), window=loop.name)
    loop.askUser = lambda question: console.ask(question, window=loop.name)
    loop.askSecret = lambda question: console.askSecret(question, window=loop.name)
    loop.askLogin = lambda host: console.say(f"{host} asks for an account. Add it in the setup of the literature reviewer next time. Skipping it.")


def buildAgent(console, swarm, spec, info, model, waitsFor=()):
    loop = buildLoop(spec["task"], model, spec["answers"])
    connect(loop, console)
    watchCosts(console, swarm, loop)
    recipe = {"task": spec["task"], "answers": publicAnswers(spec["task"], spec["answers"])}
    swarm.addAgent(spec["name"], loop, getTask(spec["task"])["role"], describeLoop(loop), waitsFor=waitsFor, model=info, recipe=recipe)
    # In a session, the agent gets its window as soon as it is in the swarm.
    if getattr(console, "hub", None) is not None:
        console.hub.follow(swarm, [])
    return loop


# An agent that joins the swarm while it runs (the command add): its task, its folder, its model, and the agents it waits for (in execute mode).
# Every agent of the swarm is told. If it cannot join (the leader already started its final work), its model is let go.
def addLive(console, swarm, specs, keys, tokens, models):
    number = len(swarm.getAgents()) + 1
    spec = fillTask(console, chooseTask(console, number, number), swarm.getAgents())
    spec["answers"]["folder"] = askFolder(console, spec, number, number)
    info, model = chooseModel(console, swarm, spec, number, number, keys, tokens)
    others = [name for name in swarm.getAgents() if name != swarm.getLeader()]
    waits = []
    if swarm.getMode() == "execute" and others:
        waits = chooseFrom(console, f"\nWhich agents must {spec['name']} wait for? It receives their results.", others, one=False, none=True, default=[],
                           hint="Your choices (numbers like 1,3; Enter or none = nobody):")
    try:
        buildAgent(console, swarm, spec, info, model, waits)
    except ValueError as error:
        console.say(f"{spec['name']} cannot join: {error}")
        if hasattr(model, "unload"):
            model.unload()
        return
    specs.append(spec)
    models[spec["name"]] = model
    console.say(f"{spec['name']} joined the swarm. Every agent was told.")


# ==============
# The leader builds the swarm (leader_utils.py): the user gives the mission, the folder of the mission and the model of the leader, and approves
# the swarm the leader proposes. While the swarm runs, the leader can propose to add or remove agents, with a reason: the user approves each change.
# ==============
def chooseBuilder(console):
    console.say("You can build the swarm yourself, agent by agent. Or a leader agent builds it: you choose its model and the folder of the mission, it proposes "
                "the agents, their tasks and their models, and you approve. While the swarm works, it can also propose to add or remove agents, always with a "
                "reason, and nothing changes before you approve.")
    options = ["I build the swarm myself", "The leader builds the swarm (I approve its proposal)"]
    return "leader" if chooseFrom(console, "Who builds the swarm?", options) == options[1] else "manual"


# What the manager of the leader needs to make an agent here (see LeaderManager): the loops of the agents it adds, or of an agent with another model.
class ConsoleMaker:
    def __init__(self, console, swarm, specs, keys, tokens, models):
        self.console, self.swarm, self.specs, self.keys, self.tokens, self.models = console, swarm, specs, keys, tokens, models

    def makeLoop(self, agent):
        model = createModel(agent["model"], self.keys, token=self.tokens.get(agent["model"]["name"]), report=self.console.say)
        loop = buildLoop(agent["task"], model, agent["answers"])
        connect(loop, self.console)
        watchCosts(self.console, self.swarm, loop)
        return loop

    def joined(self, agent, loop):
        self.specs.append({"task": agent["task"], "name": agent["name"], "answers": agent["answers"]})
        self.models[agent["name"]] = loop.agent

    def remakeLoop(self, name, model):
        spec = next((spec for spec in self.specs if spec["name"] == name), None)
        if spec is None:
            raise ValueError(f"{name} cannot be made again here.")
        return self.makeLoop({"name": name, "task": spec["task"], "answers": spec["answers"], "model": model})

    def remade(self, name, model, loop):
        self.models[name] = loop.agent


# It returns the swarm the leader built and the user approved (the leader is its first agent), or None.
def buildWithLeader(console, mission, specs, keys, tokens, models, costs):
    heading(console, "Step 1: the leader and the folder of the mission")
    console.say(LEADER_TASK["info"])
    folder = askUntilValid(console, "Folder of the mission (the agents work in it, and the final report is saved in it):",
                           lambda text: parseAnswer({"key": "folder", "ask": "", "kind": "folder", "required": True}, text))
    leader = {"task": "leader", "name": LEADER_TASK["name"], "answers": {"mission": mission, "folder": folder, "numberOfLoops": DEFAULT_LOOPS}}
    swarm = Swarm(mission)
    swarm.costs = costs
    info, model = chooseModel(console, swarm, leader, 1, 1, keys, tokens)
    models[leader["name"]] = model
    costs.track(leader["name"], info, model)
    loop = buildLoop("leader", model, leader["answers"])
    connect(loop, console)
    watchCosts(console, swarm, loop)
    loop.name = leader["name"]
    catalog = LeaderCatalog(mission, folder, leader["name"], info, keys, tokens, costs)
    heading(console, "Step 2: the leader builds the swarm")
    console.say(f"{leader['name']} is building the swarm for your mission. It shows you its proposal, and nothing is made before you approve it.")
    try:
        agents = designSwarm(loop, catalog)
    except (ValueError, ModelError) as error:
        console.say(str(error))
        return None
    if agents is None:
        console.say("You rejected the swarm of the leader.")
        return None
    buildAgent(console, swarm, leader, info, model)
    specs.append(leader)
    for agent in agents:
        spec = {"task": agent["task"], "name": agent["name"], "answers": agent["answers"]}
        models[spec["name"]] = createModel(agent["model"], keys, token=tokens.get(agent["model"]["name"]), report=console.say)
        buildAgent(console, swarm, spec, agent["model"], models[spec["name"]])
        specs.append(spec)
    for agent in agents:
        swarm.setWaitsFor(agent["name"], agent["waitsFor"])
    LeaderManager(swarm, catalog, ConsoleMaker(console, swarm, specs, keys, tokens, models))
    return swarm


def chooseOrder(console, swarm):
    workers = [name for name in swarm.getAgents() if name != swarm.getLeader()]
    if not workers:
        console.say("There is only one agent, so nobody waits for anybody.")
        return
    options = ["All the agents work at the same time", "The leader decides who waits for whom (you approve its plan)", "I choose who waits for whom"]
    picked = chooseFrom(console, "\nWho waits for whom? An agent that waits starts when the agents it waits for are done, and receives their results.", options)
    if picked == options[0]:
        for name in workers:
            swarm.setWaitsFor(name, [])
    elif picked == options[1]:
        console.say("The leader is working out the order...")
        try:
            console.say("The order is approved." if swarm.planWithLeader() else "No order was approved, so nothing changed.")
        except ModelError as error:
            console.say(f"The leader could not decide: {error}")
    else:
        for name in workers:
            others = [other for other in workers if other != name]
            if others:
                chosen = chooseFrom(console, f"\nWhich agents must {name} wait for?", others, one=False, none=True, default=[], hint="Your choices (numbers like 1,3; Enter or none = nobody):")
                swarm.setWaitsFor(name, chosen)
    try:
        swarm.getStages()
    except ValueError as error:
        console.say(f"{error} Everybody works at the same time instead.")
        for name in workers:
            swarm.setWaitsFor(name, [])
    console.say("\n" + renderTree(swarm, console.color))


def chooseMode(console):
    options = ["Plan first: every agent writes its plan, the leader summarises them, and you approve (recommended)", "Execute right away: every agent drafts its work, and you approve before it acts"]
    console.say("\nNothing is ever done without your approval: emails are only sent, events booked and files written after you approve the exact result.")
    return "plan" if chooseFrom(console, "How must the swarm start?", options) == options[0] else "execute"


def showUsage(console, models):
    lines = [f"  {name}: {model.usage['calls']} calls, {model.usage['input']:,} tokens read, {model.usage['output']:,} tokens written" for name, model in models.items() if hasattr(model, "usage")]
    if lines:
        console.say("\nTokens used (API models are billed for them):\n" + "\n".join(lines))


def report(console, swarm, outcome, models):
    heading(console, "Result")
    if "error" in outcome:
        console.say(f"The swarm could not run: {outcome['error']}")
        return False
    for name in swarm.getAgents():
        info = swarm.getInfo(name)
        detail = f"approved plan: {shorten(info['plan'], 300)}" if swarm.getMode() == "plan" and info["plan"] else f"result: {shorten(info['result'], 300)}" if info["result"] else f"problem: {info['error']}"
        console.say(f"- {name}: {info['status']}. {detail}")
    if swarm.getMode() == "plan" and len(swarm.getAgents()) > 1 and swarm.getSummary():
        console.say(f"\nThe summary plan of the leader:\n{swarm.getSummary()}")
    showUsage(console, models)
    console.say("\n" + describeCosts(swarm))
    return outcome.get("result") is not None


def runSwarm(console, swarm, resume=False):
    view = TreeView(swarm, console)
    console.commands = lambda line: runCommand(console, swarm, line)
    console.say(f"\nThe swarm {'goes on' if resume else 'starts'}. {'Type help to see what you can do while it runs.' if console.interactive else ''}\n")
    view.start()
    console.startPump()
    swarm.startInBackground(resume)
    try:
        swarm.wait()
    except KeyboardInterrupt:
        swarm.saveForExit()
        raise
    view.stop()
    console.stopPump()
    return swarm.outcome


# Runs the swarm in its mode. A plan that was approved can be executed right away. It returns the outcome of the last run.
# With resume the first run goes on where the swarm was interrupted.
def startSwarm(console, swarm, mode, models, resume=False):
    while True:
        swarm.setMode(mode)
        outcome = runSwarm(console, swarm, resume)
        resume = False
        succeeded = report(console, swarm, outcome, models)
        if mode == "plan" and succeeded and askYesNo(console, "\nThe plans are approved. Execute them now?", True):
            mode = "execute"
            continue
        return outcome


def changeModel(console, swarm, specs, models, keys, tokens):
    name = chooseFrom(console, "\nThe model of which agent do you want to change?", swarm.getAgents())
    spec, number = next(spec for spec in specs if spec["name"] == name), swarm.getAgents().index(name) + 1
    taken = swarm.getNeededVram(replacing=name)
    console.say(f"Without {name}, the swarm needs {taken} GB of VRAM.")
    info, model = chooseModel(console, swarm, spec, number, len(specs), keys, tokens, replacing=name)
    loop = buildLoop(spec["task"], model, spec["answers"])
    connect(loop, console)
    try:
        swarm.setModel(name, info, agent=loop)
    except ValueError as error:
        console.say(str(error))
        return
    old = models.get(name)
    if hasattr(old, "unload"):
        old.unload()
    models[name] = model


# ==============
# A swarm that was interrupted (the connection was lost, or the program or the computer stopped) is found again from its saved state.
# The user continues it where it stopped, or cancels it after reading what the leader says it did, or leaves it for later.
# ==============
def unloadModels(models):
    for model in models.values():
        if hasattr(model, "unload"):
            model.unload()
