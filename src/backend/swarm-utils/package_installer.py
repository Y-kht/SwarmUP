# The packages that the application cannot carry (torch alone is several gigabytes) and that a model needs: someone who only has SwarmUP.exe or
# SwarmUP.app has no pip of their own, so the application installs them with the Python it carries, in the folder of its data. Only the packages
# that the models of SwarmUP can need are installed (findMissingPackages in model_support.py), and they stay when the application is replaced.
# What they bring along never replaces what the application carries: claude-agent-sdk brought a new major version of mcp, and SwarmUP could not start
# again. So pip keeps to the versions of the application (carriedVersions), and its folder comes last on the path of Python (standalone_app.py).
import importlib
import importlib.metadata
import os
import subprocess
import sys
from pathlib import Path

import harness_utils
from model_support import ModelError


PACKAGES_FOLDER = "packages"
ALLOWED = ("torch", "transformers", "accelerate", "bitsandbytes", "anthropic", "openai", "claude-agent-sdk")
# On Windows the usual torch runs without the GPU: its builds for NVIDIA GPUs are on the page of PyTorch. On Linux they are the usual one.
TORCH_INDEX = "https://download.pytorch.org/whl/cu130"
LAST_LINES = 15
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def canInstall():
    return bool(os.environ.get("SWARMUP_STANDALONE"))


def packagesFolder():
    return Path(harness_utils.AGENT_FILES) / PACKAGES_FOLDER


# What the application carries, as lines for pip. A package that the user installed is free to change.
def carriedVersions(folder):
    versions = {}
    for package in importlib.metadata.distributions():
        path = Path(str(getattr(package, "_path", "") or ""))
        if package.metadata and package.metadata["Name"] and folder not in path.parents:
            versions[package.metadata["Name"]] = package.version
    return [f"{name}=={version}" for name, version in sorted(versions.items())]


def installCommand(names, folder, constraints, platform=sys.platform):
    extra = ["--extra-index-url", TORCH_INDEX] if platform == "win32" and "torch" in names else []
    return [sys.executable, "-m", "pip", "install", "--upgrade", "--target", str(folder), "--constraint", str(constraints), "--no-input", "--disable-pip-version-check",
            "--progress-bar", "off", *extra, *names]


# It installs the packages and says each line of pip with report(text). A package that does not install tells why, with the end of what pip said.
def installPackages(names, report=None, popen=subprocess.Popen):
    names = [str(name).strip() for name in names or []]
    unknown = [name for name in names if name not in ALLOWED]
    if unknown or not names:
        raise ValueError(f"SwarmUP does not install {', '.join(unknown) or 'nothing'}: it only installs what its models need.")
    folder = packagesFolder()
    folder.mkdir(parents=True, exist_ok=True)
    constraints = folder / "carried_versions.txt"
    constraints.write_text("\n".join(line for line in carriedVersions(folder) if line.split("==")[0].lower() not in names) + "\n", encoding="utf-8")
    process = popen(installCommand(names, folder, constraints), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True, errors="replace", creationflags=NO_WINDOW)
    said = []
    for line in process.stdout:
        said = [*said[-(LAST_LINES - 1):], line.rstrip()]
        if report is not None and line.strip():
            report(line.rstrip())
    if process.wait():
        raise ModelError(f"{', '.join(names)} could not be installed. pip said:\n" + "\n".join(said))
    if str(folder) not in sys.path:
        sys.path.append(str(folder))
    importlib.invalidate_caches()
    return names
