import json
import os

from harness_utils import AGENT_FILES


# ==============
# The settings of the user, kept in agent-files/settings.json. maxAgents is the most agents a leader that builds the swarm may put in it.
# ==============
SETTINGS_FILE = "settings.json"
DEFAULT_SETTINGS = {"maxAgents": 10}
MAX_AGENTS_LIMIT = 100


def checkSettings(settings):
    agents = settings.get("maxAgents", DEFAULT_SETTINGS["maxAgents"])
    if isinstance(agents, bool) or not isinstance(agents, int) and not (isinstance(agents, str) and agents.strip().isdigit()):
        raise ValueError("The most agents is a whole number.")
    agents = int(agents)
    if not 1 <= agents <= MAX_AGENTS_LIMIT:
        raise ValueError(f"The most agents is between 1 and {MAX_AGENTS_LIMIT}.")
    return {**DEFAULT_SETTINGS, "maxAgents": agents}


def loadSettings():
    try:
        return checkSettings(json.loads((AGENT_FILES / SETTINGS_FILE).read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, AttributeError):
        return dict(DEFAULT_SETTINGS)


def saveSettings(settings):
    settings = checkSettings({**loadSettings(), **settings})
    path = AGENT_FILES / SETTINGS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    os.replace(temporary, path)
    return settings
