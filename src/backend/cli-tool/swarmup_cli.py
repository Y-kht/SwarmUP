# SwarmUP in the command line. Run it with: python src/backend/cli-tool/swarmup_cli.py [command]
#
# A session runs the program away from the terminal (a daemon), so the swarm goes on when the terminal is closed. Terminals attach to the session
# and leave it like tmux, and the user closes it for good with kill. The commands:
#   new [--detached]          start a session and attach to it (the default command)
#   attach [session]          attach to a session: the only one, or the latest
#   list                      the sessions that run, with their state and their mission
#   status [session] [--json] where a session stands, without attaching
#   show session [window]     the latest lines of a window, without attaching
#   kill [session] [--all]    close a session (a swarm that runs is saved, and can be continued at the next start)
#   run                       the program in this terminal only, without a session (it stops with the terminal)
# The screen of a session (cli_screen.py) has a window for the swarm and one for each agent (Ctrl-b then ? for the keys).
#
# The program asks guiding questions to build a swarm, then runs it:
# 1. Who builds the swarm: you, agent by agent, or the leader (it proposes the agents, their tasks and their models, and you approve).
# 2. How many agents, and the mission of the swarm.
# 3. For every agent: its task (email, writing, coding, math checking, literature review, any work in a folder...) and what it needs to work.
# 4. For every agent: the folder it works inside (optional).
# 5. For every agent: its model, local on the GPUs (with the VRAM it needs, and a check of the GPUs), paid through an API (with the prices), or a coding agent.
# 6. The run, followed live: who waits for whom, who talks to whom, what each agent does with its tools, and what needs your approval.
# When the leader builds the swarm, steps 2 to 5 are the mission, the folder of the mission and the model of the leader: the leader does the rest,
# and while the swarm works it can propose to add or remove agents, which you approve or reject like its first proposal.
# The state of the swarm is saved all the time. If the connection is lost the swarm pauses and asks you to continue or cancel, and if the program
# or the computer stops, the next start offers to continue the swarm where it was (or to cancel it, after a summary of what it did).
# Local models are downloaded to the Hugging Face cache (the HF_HOME environment variable), and API keys come from the environment variables
# of their provider (see API_KEYS in models_library.py) or are typed here. Keys and passwords are only kept in memory.
import argparse
import json
import os
import secrets
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# The modules of SwarmUP are in the folders of src/backend. Their names have hyphens, so they are not packages: each folder goes on the path.
sys.path[:0] = [str(folder) for folder in sorted(Path(__file__).resolve().parents[1].iterdir()) if folder.is_dir() and not folder.name.startswith(("_", "."))]
from cli_hub import SWARM_WINDOW, Hub, HubConsole, startServer
from cli_screen import attach
from cli_sessions import findSession, killSession, listSessions, logPath, newSessionId, removeSession, request, startDaemon, writeSession
from internet_cache import keepFresh
from mission_costs import MissionCosts
from swarm_harness import Swarm
# The parts of the program, for the program below and for the tests and scripts that drive it (import swarmup_cli as cli).
from cli_console import Console, askUntilValid, askYesNo, chooseFrom, heading, shorten
from cli_models import chooseModel, labelLocal
from cli_program import addLive, buildAgent, buildWithLeader, changeModel, chooseBuilder, chooseMode, chooseOrder, runSwarm, startSwarm, unloadModels
from cli_resume import offerUnfinished
from cli_steps import askAgentCount, askBudget, askFolder, askMission, chooseAgent, chooseFolders, chooseTask, describeAgent, fillTask
from cli_view import TreeView, formatEvent, openAgent, renderTree, runCommand
from model_support import ModelError
from messengers import MessagingError
from models_library import MODELS_API, MODELS_LOCAL
from sources_library import NEWS_OUTLETS
from tasks_library import TASKS

KEEP_ALIVE_SECONDS = 3600


def runProgram(console):
    heading(console, "SwarmUP: build and run a swarm of agents")
    if offerUnfinished(console):
        console.say("\nBye.")
        return
    specs, keys, tokens, models = [], {}, {}, {}
    if chooseBuilder(console) == "leader":
        mission, costs = askMission(console), MissionCosts()
        askBudget(console, costs)
        swarm = buildWithLeader(console, mission, specs, keys, tokens, models, costs)
        if swarm is None:
            unloadModels(models)
            console.say("\nBye.")
            return
    else:
        count = askAgentCount(console)
        swarm = Swarm(askMission(console))
        askBudget(console, swarm.costs)
        heading(console, "Step 1: what each agent must do")
        for number in range(1, count + 1):
            specs.append(chooseAgent(console, number, count, [spec["name"] for spec in specs]))
        heading(console, "Step 2: the folder of the swarm")
        chooseFolders(console, specs)
        heading(console, "Step 3: the model of each agent")
        for number, spec in enumerate(specs, 1):
            info, model = chooseModel(console, swarm, spec, number, count, keys, tokens)
            buildAgent(console, swarm, spec, info, model)
            models[spec["name"]] = model
            status = swarm.getVramStatus()
            if status["needed"]:
                console.say(f"\nVRAM the swarm is expected to need so far: {status['needed']} GB of {status['total']} GB ({status['free']} GB free now)." + (f"\n{status['message']}" if status["message"] else ""))
    console.newAgent = lambda swarm: addLive(console, swarm, specs, keys, tokens, models)
    heading(console, "Your swarm")
    console.say(f"The first agent, {swarm.getLeader()}, is the leader: it works last, and it speaks to you for the swarm.\n\n" + renderTree(swarm, console.color))
    mode = chooseMode(console)
    while True:
        options = ["Start the swarm", "Change the model of an agent (for example to a smaller one)", "Change who waits for whom", "Show the tree again", "Quit"]
        choice = chooseFrom(console, "\nWhat now?", options)
        if choice == options[1]:
            changeModel(console, swarm, specs, models, keys, tokens)
        elif choice == options[2]:
            chooseOrder(console, swarm)
        elif choice == options[3]:
            console.say(renderTree(swarm, console.color))
        elif choice == options[4]:
            break
        else:
            outcome = startSwarm(console, swarm, mode, models)
            if "error" in outcome:
                console.say("Change a model or free some GPU memory, then start again.")
                continue
            break
    unloadModels(models)
    console.say("\nBye.")


# ==============
# The daemon of a session. It runs the program with the console of the hub, and stays after the program ended, until the user closes it.
# ==============
def serve(sessionId):
    hub = Hub(sessionId, secrets.token_urlsafe(24))
    server = startServer(hub)
    info = {"id": sessionId, "pid": os.getpid(), "port": server.server_address[1], "token": hub.token, "started": f"{datetime.now():%Y-%m-%d %H:%M:%S}",
            "folder": os.getcwd(), "log": str(logPath(sessionId))}
    writeSession({**info, **hub.describeState()})
    hub.onChange = lambda summary: writeSession({**info, **summary})
    hub.onEnd = lambda: removeSession(sessionId)
    signal.signal(signal.SIGTERM, lambda number, frame: threading.Thread(target=hub.kill, daemon=True).start())
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    keepFresh()
    try:
        runProgram(HubConsole(hub))
    except Exception as error:
        import traceback
        traceback.print_exc()
        hub.write(SWARM_WINDOW, f"The program stopped because of an error: {error}. The details are in {logPath(sessionId)}.")
    hub.finish()
    while True:
        time.sleep(KEEP_ALIVE_SECONDS)


# ==============
# The commands.
# ==============
def describeSession(session):
    waiting = f", waiting for you in {session['waiting']}" if session.get("waiting") else ""
    agents = len(session.get("agents") or [])
    mission = shorten(session.get("mission") or "(no mission yet)", 60)
    return f"{session['id']:<5} {session.get('state', '?') + waiting:<34} {agents:>2} agents  since {session.get('started', '?')}  {mission}"


def showStatus(info, asJson):
    status = request(info, {"type": "status"})
    if asJson:
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return
    print(describeSession({**info, **status}))
    if status.get("question"):
        print(f"It asks you, in window {status['question']['window']}: {status['question']['text']}")
    if status.get("tree"):
        print(f"\n{status['tree']}")
    print(f"\nWindows: {', '.join(status.get('windows') or [])}. Attach with: swarmup_cli.py attach {info['id']}")


# A window is named by its name, or by its number on the status bar (0 is the swarm).
def windowName(session, text):
    if not text.isdigit():
        return text
    names = request(session, {"type": "status"}).get("windows") or []
    return names[int(text)] if int(text) < len(names) else text


def runInTerminal():
    console = Console()
    keepFresh()
    try:
        runProgram(console)
    except (KeyboardInterrupt, EOFError):
        console.say("\nStopped by the user.")
        sys.exit(130)


def makeParser():
    parser = argparse.ArgumentParser(prog="swarmup_cli.py", description="SwarmUP in the command line. A session runs your swarm away from the terminal: "
                                     "close the terminal and it goes on, attach again from any terminal, and close it with kill.")
    commands = parser.add_subparsers(dest="command", metavar="command")
    def screenOptions(command):
        command.add_argument("--plain", action="store_true", help="show the session as plain lines instead of windows")
        command.add_argument("--prefix", default="C-b", help="the prefix key of the windows, like C-b (the default) or C-a")
    screenOptions(commands.add_parser("new", help="start a new session and attach to it (the default)"))
    commands.choices["new"].add_argument("--detached", action="store_true", help="start the session without attaching to it")
    attaching = commands.add_parser("attach", help="attach to a session that runs")
    attaching.add_argument("session", nargs="?", help="its id, like s1, or its number (the latest session if left out)")
    screenOptions(attaching)
    commands.add_parser("list", aliases=["ls"], help="list the sessions that run")
    status = commands.add_parser("status", help="where a session stands, without attaching")
    status.add_argument("session", nargs="?")
    status.add_argument("--json", action="store_true", help="as JSON, for scripts")
    show = commands.add_parser("show", help="the latest lines of a window of a session, without attaching")
    show.add_argument("session")
    show.add_argument("window", nargs="?", default=SWARM_WINDOW, help="swarm (the default), the name of an agent, or a window number")
    show.add_argument("--lines", type=int, default=200)
    kill = commands.add_parser("kill", help="close a session (a swarm that runs is saved, and can be continued at the next start)")
    kill.add_argument("session", nargs="?")
    kill.add_argument("--all", action="store_true", help="close every session")
    commands.add_parser("run", help="run SwarmUP in this terminal only, without a session: it stops when the terminal closes")
    serving = commands.add_parser("serve")
    serving.add_argument("--session", required=True)
    return parser


def main(arguments=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    options = makeParser().parse_args(arguments)
    command = options.command or "new"
    try:
        if command == "serve":
            serve(options.session)
        elif command == "run":
            runInTerminal()
        elif command == "new":
            info = startDaemon(newSessionId())
            print(f"Session {info['id']} started. It goes on when this terminal is closed; close it with: swarmup_cli.py kill {info['id']}")
            if not getattr(options, "detached", False):
                print(attach(info, getattr(options, "plain", False), getattr(options, "prefix", "C-b")))
        elif command == "attach":
            print(attach(findSession(options.session, listSessions()), options.plain, options.prefix))
        elif command in ("list", "ls"):
            sessions = listSessions()
            print("\n".join(describeSession(session) for session in sessions) if sessions else "No session runs. Start one with: swarmup_cli.py new")
        elif command == "status":
            showStatus(findSession(options.session, listSessions()), options.json)
        elif command == "show":
            session = findSession(options.session, listSessions())
            text = request(session, {"type": "capture", "window": windowName(session, options.window), "lines": options.lines}).get("text")
            print(text if text is not None else f"Session {session['id']} has no window {options.window}.")
        elif command == "kill":
            sessions = listSessions()
            for session in (sessions if options.all else [findSession(options.session, sessions)]):
                killSession(session)
                print(f"Session {session['id']} is closed." + (" Its swarm was saved: start SwarmUP again to continue it." if session.get("state") == "running" else ""))
    except (ValueError, RuntimeError, OSError) as error:
        print(error, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
