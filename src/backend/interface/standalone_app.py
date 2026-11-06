# The start of SwarmUP as an application of its own: SwarmUP.exe on Windows and SwarmUP.app on macOS (build-tools/build_executables.py). The launchers
# start this file with the Python that the application carries, and the rest is the usual window (user_interface.py). What a program started
# with a double click does not have, it gets here, before any module of SwarmUP is loaded:
# - a place for its data, since the application itself is never changed: the data of the user, their rules files and the packages they install;
# - an interpreter that child processes can start, with the certificates of its packages, and on macOS the PATH of the user, which Finder does not give;
# - a file for what it prints, since it has no terminal.
#   --self-test checks that everything works on this computer and writes a report that the user can send (standalone_checks.py).
import os
import shutil
import sys
import tempfile
from pathlib import Path


APP_NAME = "SwarmUP"
HERE = Path(__file__).resolve()
APP_ROOT = HERE.parents[3]
LOG_NAME = "app.log"
UNPACKED_FOLDER = "r"
LOGIN_SHELL_SECONDS = 5
MAC_TOOL_FOLDERS = ("/opt/homebrew/bin", "/usr/local/bin")


def dataHome():
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / APP_NAME


# The rules files can be changed and added to by the user, and the application cannot be changed: they get a copy, once. What they changed is never replaced.
def seedRules(bundled, folder):
    folder.mkdir(parents=True, exist_ok=True)
    for path in bundled.glob("*.md"):
        target = folder / path.name
        if not target.exists():
            shutil.copy2(path, target)


# The Python that child processes start: a wheel, a command of the coder, pip. The launcher itself is no interpreter, and pythonw.exe prints nothing.
def findInterpreter(root, executable, platform):
    if platform == "win32":
        return Path(executable).with_name("python.exe")
    if platform == "darwin":
        return root.parent / resourceName("python") / "bin" / "python3"
    return Path(executable)


# On macOS one application holds a Python and some packages for each kind of processor.
def resourceName(name):
    import platform
    return f"{name}-{'arm64' if platform.machine() == 'arm64' else 'x86_64'}"


# Finder gives a program only the folders of the system, so it would find neither codex nor nvidia-smi. The login shell of the user knows them.
def readLoginPath(shell, run=None):
    import subprocess
    run = run or subprocess.run
    marker = "SWARMUP_PATH="
    try:
        done = run([shell, "-ilc", f'printf "\\n{marker}%s" "$PATH"'], capture_output=True, text=True, timeout=LOGIN_SHELL_SECONDS, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    return next((line[len(marker):] for line in done.stdout.splitlines() if line.startswith(marker)), "")


def mergePaths(*lists):
    seen = []
    for folders in lists:
        for folder in str(folders or "").split(os.pathsep):
            if folder and folder not in seen:
                seen.append(folder)
    return os.pathsep.join(seen)


# SwarmUP.exe unpacks each of its versions in a folder of its own (launcher_windows.c): those of the versions before only take room. A folder that
# another start of this version is unpacking has the same name, followed by a number.
def removeOldVersions(current):
    if current.parent.name != UNPACKED_FOLDER:
        return
    for folder in current.parent.iterdir():
        if folder.name.split(".")[0] != current.name:
            shutil.rmtree(folder, ignore_errors=True)


# The output of a program without a terminal goes to a file, so its problems can be found later. A terminal keeps its output.
def openLog(home):
    folder = home / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    log = open(folder / LOG_NAME, "a", encoding="utf-8", errors="replace", buffering=1)
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if stream is None or not stream.isatty():
            setattr(sys, name, log)
    return log


def prepare():
    home = Path(os.environ.get("SWARMUP_HOME") or dataHome()).expanduser()
    home.mkdir(parents=True, exist_ok=True)
    # What only this start needs: a command of the user must find its own Python, not the one of the application.
    os.environ.pop("PYTHONHOME", None)
    os.environ.pop("PYTHONPATH", None)
    os.environ.update(SWARMUP_HOME=str(home), SWARMUP_STANDALONE="1", SWARMUP_RULES=str(home / "agent-rules"))
    sys.dont_write_bytecode = True
    openLog(home)
    seedRules(APP_ROOT / "agent-rules", home / "agent-rules")
    packages = home / "packages"
    packages.mkdir(exist_ok=True)
    # Last on the path: what a user installed adds to the application, and never replaces what it is tested with (package_installer.py).
    sys.path.append(str(packages))
    if sys.platform == "darwin":
        resources = APP_ROOT.parent
        sys.path[1:1] = [str(resources / resourceName("packages")), str(resources / "packages")]
        os.environ["PATH"] = mergePaths(readLoginPath(os.environ.get("SHELL") or "/bin/zsh"), os.environ.get("PATH"), os.pathsep.join(MAC_TOOL_FOLDERS))
    sys.executable = str(findInterpreter(APP_ROOT, sys.executable, sys.platform))
    removeOldVersions(APP_ROOT.parent)
    try:
        import certifi
        os.environ.setdefault("SSL_CERT_FILE", certifi.where())
    except ImportError:
        pass
    os.chdir(home)
    return home


def main(arguments=None):
    arguments = sys.argv[1:] if arguments is None else arguments
    home = prepare()
    sys.path.insert(0, str(HERE.parent))
    if "--self-test" in arguments:
        # Its swarm is a test, and the missions of the user are not its place: it has a home of its own, which goes with it.
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary:
            os.environ["SWARMUP_HOME"] = temporary
            from standalone_checks import runSelfTest
            return runSelfTest(home, APP_ROOT)
    from user_interface import main as openWindow
    openWindow()
    return 0


if __name__ == "__main__":
    sys.exit(main())
