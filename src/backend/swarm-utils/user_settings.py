import json
import os

from harness_utils import AGENT_FILES


# ==============
# The settings of the user, kept in agent-files/settings.json:
# - maxAgents: the most agents a leader that builds the swarm may put in it.
# - maxTools: the most tools one agent is given at a time (the rest is cut, and the user is told).
# - architectShare: the part of the budget of a mission, in percent, that the architect may spend; architectDollars: what it may spend in a
#   mission without a budget.
# ==============
SETTINGS_FILE = "settings.json"
DEFAULT_SETTINGS = {"maxAgents": 10, "maxTools": 1000, "architectShare": 10, "architectDollars": 1.0}
MAX_AGENTS_LIMIT = 100
MAX_TOOLS_LIMIT = 100000
# key: (whole number, lowest, highest, its name for the user).
LIMITS = {"maxAgents": (True, 1, MAX_AGENTS_LIMIT, "The most agents"), "maxTools": (True, 10, MAX_TOOLS_LIMIT, "The most tools of an agent"),
          "architectShare": (True, 0, 100, "The share of the architect"), "architectDollars": (False, 0, 1000, "The dollars of the architect")}


# A setting with a wrong value: key says which one.
class SettingError(ValueError):
    def __init__(self, key, text):
        super().__init__(text)
        self.key = key


def readNumber(key, value):
    whole, lowest, highest, label = LIMITS[key]
    text = str(value).strip()
    try:
        if isinstance(value, bool):
            raise ValueError
        number = int(text) if whole else float(text)
    except ValueError:
        raise SettingError(key, f"{label} is a whole number." if whole else f"{label} is a number.") from None
    if not lowest <= number <= highest:
        raise SettingError(key, f"{label} is between {lowest} and {highest}.")
    return number


def checkSettings(settings):
    return {key: readNumber(key, settings.get(key, default)) for key, default in DEFAULT_SETTINGS.items()}


def loadSettings():
    try:
        return checkSettings(json.loads((AGENT_FILES / SETTINGS_FILE).read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, AttributeError):
        return dict(DEFAULT_SETTINGS)


# The settings read again only when their file changed: the gateway reads maxTools at every step of every agent.
class SettingsCache:
    def __init__(self):
        self.key, self.settings = None, None

    def get(self):
        path = AGENT_FILES / SETTINGS_FILE
        try:
            key = (str(path), path.stat().st_mtime_ns)
        except OSError:
            key = (str(path), None)
        if key != self.key:
            self.key, self.settings = key, loadSettings()
        return self.settings


SETTINGS = SettingsCache()


def currentSetting(key):
    return SETTINGS.get().get(key, DEFAULT_SETTINGS[key])


def saveSettings(settings):
    settings = checkSettings({**loadSettings(), **settings})
    path = AGENT_FILES / SETTINGS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    os.replace(temporary, path)
    return settings
