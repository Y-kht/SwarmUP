# The wheels of an agent as tools (server_wheels.py): one tool for each wheel its leader gave it, at the version it was shown.
# A script wheel runs in a process of its own (wheel_runner.py), in the folder of the agent, like a command: with progress, a time limit, and
# a stop of the swarm that ends it at once. The user allows each version of a wheel (its pin) once, always if they like (mcp_policy.py).
# A wheel of steps calls the tools of the agent, one after the other, through the gateway: each step is asked and recorded like a call of the
# agent itself. What a wheel gives back is data, never instructions. When a wheel fails, the agent is told what to do, and the journal keeps
# the details for the architect, who fixes the wheel.
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from agent_tools import Toolbox
from mcp_kit import DESCRIPTION_CHARS, ToolFailure, ToolProblem, ToolSpec, clip, fullName
from mcp_policy import decidePermission
from project_identity import projectFor
from wheel_store import keepVersion, loadWheel, resolve


SERVER = "wheels"
DEFAULT_SECONDS = 120
MAX_SECONDS = 600
CODE_LINES = 80
STEP_CHARS = 4000
RUNNER = Path(__file__).resolve().with_name("wheel_runner.py")
TEMPLATE = re.compile(r"\{(\w+)\}")


def projectOfLoop(loop):
    project = getattr(getattr(loop, "team", None), "project", None)
    return project or projectFor(getattr(loop, "folder", None))[0]


# The tools of the wheels of the agent. Listing them pins the version of each wheel; a call runs the pinned version.
def provideWheels(agent, listing):
    loop = agent.loop
    names = list(getattr(loop, "wheels", None) or [])
    if not names:
        agent.wheelPins = {}
        return []
    pins = getattr(agent, "wheelPins", None)
    if listing or not pins:
        wheels, missing = resolve(names, projectOfLoop(loop))
        for wheel in wheels:
            keepVersion(wheel)
        agent.wheelPins = {wheel["name"]: wheel["pin"] for wheel in wheels}
    else:
        wheels = [wheel for wheel in (loadWheel(name, pin) for name, pin in pins.items()) if wheel is not None]
    return [toolOf(wheel) for wheel in wheels]


def describeTool(wheel):
    manifest = wheel["manifest"]
    effects = []
    if manifest.get("changes"):
        effects.append("it changes files of your folder")
    if manifest.get("network"):
        effects.append("it reaches the internet")
    how = "It runs in your folder" if wheel["kind"] == "script" else "It calls your tools for you, each one asked and recorded as yours"
    tail = (f" A wheel (v{wheel['version']}): the architect wrote it from earlier work. {how}" + (f"; {' and '.join(effects)}" if effects else "") +
            ". The user approves each new version of a wheel once.")
    summary, room = str(manifest.get("summary") or wheel.get("summary") or "").strip(), DESCRIPTION_CHARS - len(tail)
    return (summary if len(summary) <= room else summary[:room - 3] + "...") + tail


def toolOf(wheel):
    manifest = wheel["manifest"]
    properties = {key: dict(value) for key, value in (manifest.get("inputs") or {}).items() if isinstance(value, dict)}
    seconds = min(int(manifest.get("timeout") or DEFAULT_SECONDS), MAX_SECONDS)
    if wheel["kind"] == "steps":
        run = lambda work, **arguments: runSteps(work, wheel, arguments)
    else:
        run = lambda work, **arguments: runScript(work, wheel, arguments, seconds)
    return ToolSpec(wheel["name"], describeTool(wheel), properties, manifest.get("required") or [], run, readOnly=False,
                    destructive=bool(manifest.get("changes")), idempotent=False, openWorld=True, timeout=seconds + 30)


def describeCode(wheel):
    lines = wheel["text"].splitlines()
    shown = "\n".join(lines[:CODE_LINES]) + (f"\n... and {len(lines) - CODE_LINES} more lines" if len(lines) > CODE_LINES else "")
    return shown


def askToRun(box, work, wheel, arguments):
    manifest = wheel["manifest"]
    flags = [text for flag, text in (("changes", "It changes files."), ("network", "It reaches the internet.")) if manifest.get(flag)]
    detail = (f"{manifest.get('summary') or ''}\nArguments: {json.dumps(arguments, ensure_ascii=False)[:1500]}\n{' '.join(flags)}\n"
              f"The code of version {wheel['version']}:\n{describeCode(wheel)}").strip()
    request = {"action": f"run the wheel {wheel['name']}", "detail": detail, "folder": str(box.root), "reason": "", "key": f"wheel:{wheel['pin']}", "place": "",
               "pin": wheel["pin"]}
    answer = work.permit(request) if work is not None and work.session is not None else decidePermission(box.loop, request)
    if answer.get("decision") not in ("once", "run"):
        why = f" They said: {answer['message']}" if answer.get("message") else ""
        raise ToolProblem(f"The user refused to let you run the wheel {wheel['name']}.{why} Do the step by hand, or ask your leader with team__send_message.")


def scratchFolder(loop):
    session = getattr(loop, "session", None)
    folder = Path(session.temp) / "wheels" if session is not None else Path(tempfile.gettempdir()) / "swarmup-wheels"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def commandLine(parts):
    return subprocess.list2cmdline(parts) if os.name == "nt" else shlex.join(parts)


def runScript(work, wheel, arguments, seconds):
    loop = work.loop
    box = Toolbox(loop, work)
    askToRun(box, work, wheel, arguments)
    folder = scratchFolder(loop)
    stamp = f"{wheel['pin'].replace('@', '_')}_{os.getpid()}_{time.monotonic_ns()}"
    script, argumentsFile, resultFile = folder / f"{wheel['pin'].replace('@', '_')}.py", folder / f"{stamp}.args.json", folder / f"{stamp}.result.json"
    script.write_text(wheel["text"], encoding="utf-8")
    argumentsFile.write_text(json.dumps(arguments, ensure_ascii=False), encoding="utf-8")
    started = time.time()
    try:
        code, printed = box.runInForeground(commandLine([sys.executable, "-I", str(RUNNER), str(script), str(argumentsFile), str(resultFile)]), box.root, seconds)
        result = json.loads(resultFile.read_text(encoding="utf-8")) if resultFile.exists() else None
    finally:
        argumentsFile.unlink(missing_ok=True)
        resultFile.unlink(missing_ok=True)
    spent = round(time.time() - started, 3)
    if code is None:
        loop.note("wheel", name=wheel["name"], pin=wheel["pin"], action="run", ok=False, error={"type": "TimeLimit", "message": f"more than {seconds} s"}, seconds=spent)
        raise ToolFailure(f"The wheel {wheel['name']} ran for more than {seconds} seconds and was stopped. Do the step by hand, and say so in your answer.")
    if result is None:
        loop.note("wheel", name=wheel["name"], pin=wheel["pin"], action="run", ok=False, error={"type": "NoResult", "message": f"exit code {code}"}, trace=printed[-3000:], seconds=spent)
        raise ToolFailure(f"The wheel {wheel['name']} stopped before it finished (exit code {code}). Do the step by hand, and say so in your answer.")
    loop.note("wheel", name=wheel["name"], pin=wheel["pin"], action="run", ok=result["ok"], error=result.get("error"), trace=(result.get("trace") or "")[-3000:],
              seconds=spent)
    if not result["ok"]:
        error = result.get("error") or {}
        raise ToolFailure(f"The wheel {wheel['name']} failed: {error.get('type')}: {error.get('message')}. It may not fit this case: do the step by hand, "
                          "and say so in your answer.")
    loop.logAction(f"Ran the wheel {wheel['name']} (v{wheel['version']}) in {box.root}.")
    output = result.get("output") or "Done."
    printedText = (result.get("printed") or "").strip()
    text = output + (f"\n\nIt printed:\n{printedText[-4000:]}" if printedText else "")
    return f"<data from=\"wheel {wheel['name']}\">\n{text}\n</data>"


# A value of the arguments of a step, with the inputs of the wheel and what the steps before saved: "{name}" alone keeps the value as it is.
def fill(value, values):
    if isinstance(value, str):
        whole = TEMPLATE.fullmatch(value)
        if whole and whole.group(1) in values:
            return values[whole.group(1)]
        return TEMPLATE.sub(lambda match: str(values.get(match.group(1), match.group(0))), value)
    if isinstance(value, dict):
        return {key: fill(item, values) for key, item in value.items()}
    if isinstance(value, list):
        return [fill(item, values) for item in value]
    return value


def runSteps(work, wheel, arguments):
    loop = work.loop
    link = work.agent.gateway.link(loop) if work.agent.gateway is not None else None
    if link is None:
        raise ToolProblem(f"The wheel {wheel['name']} calls tools, and works only inside a swarm.")
    values, shown, started = dict(arguments), [], time.time()
    for number, step in enumerate(wheel["manifest"].get("steps") or [], 1):
        work.checkCancelled()
        tool = str(step.get("tool") or "")
        text, error = link.call(tool, fill(step.get("arguments") or {}, values), callId=f"{wheel['name']}.{number}", origin=f"wheel:{wheel['name']}")
        if error:
            loop.note("wheel", name=wheel["name"], pin=wheel["pin"], action="run", ok=False, error={"type": "Step", "message": f"step {number} ({tool}): {text[:500]}"},
                      seconds=round(time.time() - started, 3))
            done = "\n".join(shown)
            raise ToolFailure(f"The wheel {wheel['name']} stopped at step {number} ({tool}): {clip(text, 1500)}\n" + (f"What the steps before gave:\n{done}\n" if done else "") +
                              "Do the rest by hand, and say so in your answer.")
        for name, pattern in (step.get("save") or {}).items():
            match = re.search(str(pattern), text)
            if match:
                values[name] = match.group(1) if match.groups() else match.group(0)
        shown.append(f"Step {number} ({tool}):\n{clip(text, STEP_CHARS)}")
    loop.note("wheel", name=wheel["name"], pin=wheel["pin"], action="run", ok=True, seconds=round(time.time() - started, 3))
    return "\n\n".join(shown) or "Done: the wheel has no steps."


def isWheelTool(name):
    return str(name).startswith(fullName(SERVER, ""))
