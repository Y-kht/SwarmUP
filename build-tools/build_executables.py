# Builds the two applications of standalone-executables on any Linux computer, with no Windows and no Mac: each one holds a real Python, its packages
# and the code of SwarmUP as it is in this repository, so wheels, pip and the paths of the program work as they do from the sources.
#   python build-tools/build_executables.py [--only windows|macos] [--work FOLDER] [--output FOLDER]
# It needs Python 3.12 (the compiled files must fit the Python of the applications) and the internet. Every download is checked against its hash.
# SwarmUP.exe is a small program (launcher_windows.c) with a zip behind it. SwarmUP.app.zip holds a usual Mac application, for both kinds of processor.
import argparse
import compileall
import filecmp
import hashlib
import os
import plistlib
import py_compile
import shutil
import stat
import struct
import subprocess
import sys
import tarfile
import urllib.request
import venv
import zipfile
from pathlib import Path


HERE = Path(__file__).resolve().parent
REPOSITORY = HERE.parent
VERSION = "1.0.0"
BUNDLE_ID = "io.github.y-kht.swarmup"
PBS_RELEASE = "https://github.com/astral-sh/python-build-standalone/releases/download/20261026/"
PBS_NAME = "cpython-3.12.16%2B20261026-{}-install_only_stripped.tar.gz"
PYTHONS = {"windows": ("x86_64-pc-windows-msvc", "1f2ab53880fc67ea323398c6388e05af28addc99a8e389209f08a51fdff4b149"),
           "arm64": ("aarch64-apple-darwin", "353d5f1b4fd2715e7053f12f068435d5bbad16b91e057006b5d01e0925709a7e"),
           "x86_64": ("x86_64-apple-darwin", "37c38fda76fec1fbf310adcbafb7f0d96af13b5511bf8ea954c3aa8eec185734")}
MINIZ = ("https://github.com/richgel999/miniz/releases/download/3.0.2/miniz-3.0.2.zip", "ada38db0b703a56d3dd6d57bf84a9c5d664921d870d8fea4db153979fb5332c5")
RCODESIGN = ("https://github.com/indygreg/apple-platform-rs/releases/download/apple-codesign%2F0.29.0/apple-codesign-0.29.0-x86_64-unknown-linux-musl.tar.gz", "")
BUILD_PACKAGES = ("uv==0.13.7", "ziglang==0.17.1")
# Made once from src/user-interface/logo.svg, with cairosvg and Pillow.
ICONS = HERE / "icons"
# A file that GitHub still accepts in a repository.
MOST_BYTES = 100 * 1000 * 1000
# Windows reads a path of 259 characters at most, and the folder of the user comes before this.
MOST_PATH = 190
WHEEL_PLATFORMS = {"windows": "x86_64-pc-windows-msvc", "arm64": "aarch64-apple-darwin", "x86_64": "x86_64-apple-darwin"}
# What neither the program nor a wheel needs: the tests and the graphical tools of Python, and files for those who build Python.
PYTHON_TRASH = ("test", "idlelib", "tkinter", "turtledemo", "lib2to3", "turtle.py", "pydoc_data", "ensurepip", "__phello__", "lib-tk", "unittest/test")
ROOT_TRASH = ("include", "share", "tcl", "lib/tcl8", "lib/tcl8.6", "lib/tk8.6", "lib/thread2.8.7", "lib/itcl4.2.3", "lib/pkgconfig", "man", "Scripts")
PACKAGE_TRASH = ("PyObjCTest", "bin", "Scripts", "__pycache__", "pip/_vendor/distlib/w32.exe")
TRASH_MATCHES = ("_tkinter*", "_test*", "xx*", "libtcl*", "libtk*", "tcl*.dll", "tk*.dll", "zlib*.dll.tmp")
MAC_BIN_KEPT = ("python3", "python3.12", "python")
LAUNCHER_FLAGS = ("-Os", "-s", "-Wno-everything")
PLIST = {"CFBundleName": "SwarmUP", "CFBundleDisplayName": "SwarmUP", "CFBundleExecutable": "SwarmUP", "CFBundleIdentifier": BUNDLE_ID, "CFBundlePackageType": "APPL",
         "CFBundleShortVersionString": VERSION, "CFBundleVersion": VERSION, "CFBundleIconFile": "SwarmUP.icns", "CFBundleInfoDictionaryVersion": "6.0", "LSMinimumSystemVersion": "11.0",
         "NSHighResolutionCapable": True, "NSHumanReadableCopyright": "Licensed under the Apache License 2.0",
         "NSDesktopFolderUsageDescription": "Your agents work in the folders you choose.", "NSDocumentsFolderUsageDescription": "Your agents work in the folders you choose.",
         "NSDownloadsFolderUsageDescription": "Your agents work in the folders you choose.", "NSRemovableVolumesUsageDescription": "Your agents work in the folders you choose.",
         "NSNetworkVolumesUsageDescription": "Your agents work in the folders you choose."}
WINDOWS_GUI = 2
MACHO_FAT = 0xcafebabe
MACHO_CPU = {0x0100000c: "arm64", 0x01000007: "x86_64"}


def say(text):
    print(f"[build] {text}", flush=True)


def download(url, target, sha256=""):
    if not target.exists():
        say(f"downloading {target.name}")
        temporary = target.with_suffix(target.suffix + ".part")
        with urllib.request.urlopen(url, timeout=300) as answer, open(temporary, "wb") as file:
            shutil.copyfileobj(answer, file)
        temporary.rename(target)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    wanted = sha256 or urllib.request.urlopen(url + ".sha256", timeout=60).read().decode().split()[0]
    if digest != wanted:
        target.unlink()
        raise SystemExit(f"{target.name} is not the file it should be: {digest}, and not {wanted}.")
    return target


class Tools:
    def __init__(self, work):
        self.folder = work / "tools"
        self.folder.mkdir(parents=True, exist_ok=True)
        environment = self.folder / "venv"
        if not (environment / "bin" / "python").exists():
            say("preparing the tools of the build")
            venv.create(environment, with_pip=True)
            subprocess.run([environment / "bin" / "pip", "install", "-q", *BUILD_PACKAGES], check=True)
        self.python = environment / "bin" / "python"
        self.uv = environment / "bin" / "uv"

    def zig(self, *arguments, cwd=None):
        subprocess.run([self.python, "-m", "ziglang", *arguments], check=True, cwd=cwd)

    def miniz(self):
        folder = self.folder / "miniz"
        if not (folder / "miniz.c").exists():
            with zipfile.ZipFile(download(MINIZ[0], self.folder / "miniz.zip", MINIZ[1])) as archive:
                archive.extractall(folder)
        return folder

    def rcodesign(self):
        program = self.folder / "rcodesign"
        if not program.exists():
            with tarfile.open(download(RCODESIGN[0], self.folder / "rcodesign.tar.gz")) as archive:
                member = next(item for item in archive.getmembers() if item.name.endswith("/rcodesign"))
                member.name = "rcodesign"
                archive.extract(member, self.folder, filter="data")
        return program


def unpackPython(work, key, target):
    name = PYTHONS[key][0]
    archive = download(PBS_RELEASE + PBS_NAME.format(name), work / "downloads" / f"{name}.tar.gz", PYTHONS[key][1])
    target.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as files:
        files.extractall(target.parent, filter="tar")
    (target.parent / "python").rename(target)


def removeEach(folder, names):
    for name in names:
        path = folder / name
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()


def trimPython(root, standard):
    removeEach(root, ROOT_TRASH)
    removeEach(standard, PYTHON_TRASH)
    for pattern in TRASH_MATCHES:
        for path in [*root.glob(f"DLLs/{pattern}"), *root.glob(f"lib/{pattern}"), *standard.glob(f"lib-dynload/{pattern}"), *root.glob(pattern)]:
            removeEach(path.parent, [path.name])
    for folder in [*root.rglob("__pycache__")]:
        shutil.rmtree(folder, ignore_errors=True)
    for path in [*standard.glob("config-*")]:
        shutil.rmtree(path, ignore_errors=True)


def installPackages(tools, key, requirements, target):
    say(f"installing the packages for {key}")
    target.mkdir(parents=True, exist_ok=True)
    subprocess.run([tools.uv, "pip", "install", "-q", "--target", target, "--python-platform", WHEEL_PLATFORMS[key], "--python-version", "3.12", "--only-binary", ":all:",
                    "--no-binary", "proxy-tools", "--no-deps", "-r", requirements], check=True)
    removeEach(target, PACKAGE_TRASH)
    for folder in [*target.rglob("__pycache__"), *target.rglob("*.dSYM")]:
        shutil.rmtree(folder, ignore_errors=True)


# The code of SwarmUP, as in this repository: its paths stay the same as they are from the sources.
def copyProgram(target):
    target.mkdir(parents=True)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(REPOSITORY / "src", target / "src", ignore=ignore)
    shutil.copytree(REPOSITORY / "agent-rules", target / "agent-rules", ignore=ignore)
    for name in ("LICENSE", "NOTICE"):
        shutil.copy2(REPOSITORY / name, target / name)


# Compiled files that are never compared with their sources: the program starts fast, and nothing is written in an application that must not change.
def compileFolder(folder):
    if not compileall.compile_dir(str(folder), quiet=2, optimize=0, invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH, workers=0, legacy=False, force=True):
        say(f"note: some files of {folder.name} do not compile (they are not for this Python)")


def zipFolder(folder, target, base=None):
    base = base or folder
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(folder.rglob("*")):
            name = path.relative_to(base).as_posix()
            if path.is_symlink():
                info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o755) << 16
                archive.writestr(info, os.readlink(path))
            elif path.is_file():
                info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (stat.S_IFREG | (path.stat().st_mode & 0o777)) << 16
                with open(path, "rb") as source, archive.open(info, "w", force_zip64=True) as written:
                    shutil.copyfileobj(source, written)


def checkSize(path):
    size = path.stat().st_size
    say(f"{path.name}: {size / 1e6:.1f} MB")
    if size >= MOST_BYTES:
        raise SystemExit(f"{path.name} has {size / 1e6:.1f} MB, and GitHub refuses a file of 100 MB or more.")


# ==============
# Windows: one .exe.
# ==============
def compileWindowsLauncher(tools, work):
    miniz = tools.miniz()
    target = work / "SwarmUP-stub.exe"
    resources = work / "resources.rc"
    resources.write_text(f'1 ICON "{ICONS / "SwarmUP.ico"}"\n1 VERSIONINFO\nFILEVERSION {VERSION.replace(".", ",")},0\nPRODUCTVERSION {VERSION.replace(".", ",")},0\nBEGIN\n'
                         'BLOCK "StringFileInfo"\nBEGIN\nBLOCK "040904B0"\nBEGIN\n'
                         f'VALUE "ProductName", "SwarmUP"\nVALUE "FileDescription", "SwarmUP"\nVALUE "ProductVersion", "{VERSION}"\nVALUE "FileVersion", "{VERSION}"\n'
                         'VALUE "LegalCopyright", "Licensed under the Apache License 2.0"\nEND\nEND\nBLOCK "VarFileInfo"\nBEGIN\nVALUE "Translation", 0x409, 1200\nEND\nEND\n', encoding="utf-8")
    tools.zig("cc", "-target", "x86_64-windows-gnu", *LAUNCHER_FLAGS, "-municode", "-Wl,--subsystem,windows", f"-I{miniz}", "-DMINIZ_NO_TIME", str(HERE / "launcher_windows.c"), str(miniz / "miniz.c"),
              resources.name, "-lshell32", "-lgdi32", "-luser32", "-o", str(target), cwd=work)
    checkWindowsProgram(target)
    return target


# A program of the console subsystem opens a black window beside SwarmUP at each start: it must be a program of the graphical one.
def checkWindowsProgram(path):
    data = path.read_bytes()
    header = struct.unpack("<I", data[0x3c:0x40])[0]
    subsystem = struct.unpack("<H", data[header + 24 + 68:header + 24 + 70])[0]
    if subsystem != WINDOWS_GUI:
        raise SystemExit(f"{path.name} is a program of the subsystem {subsystem}, and not of the graphical one: it would open a console.")


def buildWindows(tools, work, output):
    root = work / "windows"
    shutil.rmtree(root, ignore_errors=True)
    python = root / "python"
    unpackPython(work, "windows", python)
    trimPython(python, python / "Lib")
    installPackages(tools, "windows", HERE / "requirements_windows.txt", python / "Lib" / "site-packages")
    copyProgram(root / "app")
    say("compiling for Windows")
    compileFolder(python)
    compileFolder(root / "app")
    longest = max((len(str(path.relative_to(root))) for path in root.rglob("*")), default=0)
    if longest > MOST_PATH:
        raise SystemExit(f"The longest path of the application has {longest} characters: on Windows it would not open.")
    say(f"zipping for Windows (the longest path has {longest} characters)")
    payload = work / "payload.zip"
    zipFolder(root, payload)
    stub = compileWindowsLauncher(tools, work)
    data = payload.read_bytes()
    identity = hashlib.sha256(data).hexdigest()[:8]
    target = output / "SwarmUP.exe"
    with open(target, "wb") as file:
        offset = file.write(stub.read_bytes())
        file.write(data)
        file.write(b"SWARMUP1" + struct.pack("<QQ", offset, len(data)) + identity.encode("ascii") + b"\0" * 8)
    checkSize(target)
    return target


# ==============
# macOS: one application, for both kinds of processor, in a zip.
# ==============
def machoArchitectures(path):
    try:
        with open(path, "rb") as file:
            head = file.read(8)
            if len(head) < 8:
                return set()
            magic = struct.unpack(">I", head[:4])[0]
            if magic == MACHO_FAT:
                count = struct.unpack(">I", head[4:])[0]
                return {MACHO_CPU.get(struct.unpack(">I", file.read(20)[:4])[0], "other") for _ in range(count)}
            if struct.unpack("<I", head[:4])[0] == 0xfeedfacf:
                return {MACHO_CPU.get(struct.unpack("<i", head[4:])[0] & 0xffffffff, "other")}
    except OSError:
        pass
    return set()


# Each binary must run on the processors of its folder: the packages that both use must have both.
def checkArchitectures(folder, needed):
    wrong = []
    for path in folder.rglob("*"):
        if path.is_file() and not path.is_symlink():
            found = machoArchitectures(path)
            if found and not set(needed) <= found:
                wrong.append(f"{path.relative_to(folder)}: {sorted(found)}")
    if wrong:
        raise SystemExit(f"These binaries of {folder.name} do not run on {needed}:\n" + "\n".join(wrong[:20]))


# What is the same for both processors goes once in packages; the rest (a few packages) goes in a folder for each.
def splitPackages(first, second, shared):
    shared.mkdir()
    for item in sorted(first.iterdir()):
        twin = second / item.name
        same = twin.exists() and (filecmp.cmp(item, twin, shallow=False) if item.is_file() else not differingTree(item, twin))
        if same:
            shutil.move(item, shared / item.name)
            removeEach(second, [item.name])


def differingTree(first, second):
    names = {path.relative_to(first) for path in first.rglob("*")} | {path.relative_to(second) for path in second.rglob("*")}
    for name in names:
        one, two = first / name, second / name
        if one.is_dir() and two.is_dir():
            continue
        if not one.is_file() or not two.is_file() or not filecmp.cmp(one, two, shallow=False):
            return True
    return False


def compileMacLauncher(tools, work):
    slices = []
    for architecture, target in (("arm64", "aarch64-macos.11.0"), ("x86_64", "x86_64-macos.11.0")):
        path = work / f"SwarmUP-{architecture}"
        tools.zig("cc", "-target", target, *LAUNCHER_FLAGS, str(HERE / "launcher_macos.c"), "-o", str(path))
        slices.append((architecture, path.read_bytes()))
    return makeUniversal(slices)


# The two programs in one file: a table of where each one starts, each at a page of 16 KB.
def makeUniversal(slices):
    align, offset, table, body = 14, 4096, [], b""
    cpu = {name: number for number, name in MACHO_CPU.items()}
    for name, data in slices:
        offset += (-offset) % (1 << align)
        table.append(struct.pack(">IIIII", cpu[name], 3 if name == "arm64" else 3, offset, len(data), align))
        body += b"\0" * (offset - 4096 - len(body)) + data
        offset += len(data)
    head = struct.pack(">II", MACHO_FAT, len(slices)) + b"".join(table)
    return head + b"\0" * (4096 - len(head)) + body


def buildMacos(tools, work, output):
    root = work / "macos"
    shutil.rmtree(root, ignore_errors=True)
    application = root / "SwarmUP.app" / "Contents"
    resources = application / "Resources"
    (application / "MacOS").mkdir(parents=True)
    resources.mkdir()
    for architecture in ("arm64", "x86_64"):
        python = resources / f"python-{architecture}"
        unpackPython(work, architecture, python)
        trimPython(python, python / "lib" / "python3.12")
        removeEach(python / "bin", [path.name for path in (python / "bin").iterdir() if path.name not in MAC_BIN_KEPT])
        installPackages(tools, architecture, HERE / "requirements_macos.txt", resources / f"packages-{architecture}")
    splitPackages(resources / "packages-arm64", resources / "packages-x86_64", resources / "packages")
    checkArchitectures(resources / "packages", ("arm64", "x86_64"))
    for architecture in ("arm64", "x86_64"):
        checkArchitectures(resources / f"python-{architecture}", (architecture,))
        checkArchitectures(resources / f"packages-{architecture}", (architecture,))
        compileFolder(resources / f"python-{architecture}")
        compileFolder(resources / f"packages-{architecture}")
    compileFolder(resources / "packages")
    copyProgram(resources / "app")
    compileFolder(resources / "app")
    (application / "MacOS" / "SwarmUP").write_bytes(compileMacLauncher(tools, work))
    (application / "MacOS" / "SwarmUP").chmod(0o755)
    shutil.copy2(ICONS / "SwarmUP.icns", resources / "SwarmUP.icns")
    (application / "Info.plist").write_bytes(plistlib.dumps(PLIST))
    (application / "PkgInfo").write_text("APPL????", encoding="ascii")
    say("signing for macOS, without a certificate")
    subprocess.run([tools.rcodesign(), "sign", root / "SwarmUP.app"], check=True, stdout=subprocess.DEVNULL)
    say("zipping for macOS")
    target = output / "SwarmUP.app.zip"
    zipFolder(root / "SwarmUP.app", target, root)
    checkSize(target)
    return target


def main():
    parser = argparse.ArgumentParser(description="Builds the applications of standalone-executables.")
    parser.add_argument("--only", choices=("windows", "macos"))
    parser.add_argument("--work", default="", help="the folder for the build (a new one by default)")
    parser.add_argument("--output", default=str(REPOSITORY / "standalone-executables"))
    options = parser.parse_args()
    if sys.version_info[:2] != (3, 12):
        raise SystemExit("The build needs Python 3.12, the Python of the applications, to compile their files.")
    work = Path(options.work or Path.home() / ".cache" / "swarmup-build").expanduser().resolve()
    (work / "downloads").mkdir(parents=True, exist_ok=True)
    output = Path(options.output).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    tools = Tools(work)
    if options.only != "macos":
        buildWindows(tools, work, output)
    if options.only != "windows":
        buildMacos(tools, work, output)


if __name__ == "__main__":
    main()
