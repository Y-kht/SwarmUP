# Tests replace a function or a value of SwarmUP for a while. A module of SwarmUP that is split in several modules is a family: what a test
# replaces in the module is replaced in every module of its family that uses the same thing, as when the family was a single module.
# For anything else (a class, an object, a module of Python) everywhere is mock.patch.object.
import importlib
import types
from unittest import mock

FAMILIES = {
    "model_clients": ["model_clients", "model_support", "coding_agents", "codex_agent"],
    "harness_utils": ["harness_utils", "message_loops", "writing_loops", "checking_loops", "base_loop", "swarm_review", "internet_cache", "gpu_check", "messengers", "saved_swarms", "swarm_run", "swarm_harness", "mission_costs", "user_settings", "swarm_team"],
    "leader_utils": ["leader_utils", "leader_manager", "leader_parser", "leader_catalog", "leader_checks"],
    "user_interface": ["user_interface", "web_server", "session_core", "interface_views", "session_models", "session_saved", "session_steps", "session_runs"],
}
MISSING = object()


class SharedPatch:
    def __init__(self, patches, new):
        self.patches, self.new = patches, new

    def start(self):
        for patch in self.patches:
            patch.start()
        return self.new

    def stop(self):
        for patch in reversed(self.patches):
            patch.stop()

    def __enter__(self):
        return self.start()

    def __exit__(self, *problem):
        self.stop()
        return False


def everywhere(target, name, new=mock.DEFAULT, **options):
    family = FAMILIES.get(target.__name__) if isinstance(target, types.ModuleType) else None
    if not family:
        return mock.patch.object(target, name, new, **options)
    modules = [importlib.import_module(module) for module in family]
    original = getattr(target, name, MISSING)
    if original is MISSING:
        original = next((getattr(module, name) for module in modules if hasattr(module, name)), MISSING)
    if original is MISSING and not options.get("create"):
        raise AttributeError(f"{target.__name__} and the modules made from it have no {name}")
    holders = modules if original is MISSING else [module for module in modules if getattr(module, name, MISSING) is original]
    create = options.pop("create", False)
    if new is mock.DEFAULT:
        new = mock.MagicMock(**options)
    return SharedPatch([mock.patch.object(module, name, new, create=create) for module in holders], new)
