import importlib
import json
import os
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
from patching import everywhere
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# The modules of SwarmUP are in the folders of src/backend. Their names have hyphens, so they are not packages: each folder goes on the path.
sys.path[:0] = [str(folder) for folder in sorted((Path(__file__).resolve().parent.parent / "src" / "backend").iterdir()) if folder.is_dir() and not folder.name.startswith(("_", "."))]
import harness_utils
import package_installer
import standalone_app
import user_interface
from model_support import ModelError

REPOSITORY = Path(__file__).resolve().parent.parent
OUTPUT = REPOSITORY / "standalone-executables"
sys.path.append(str(REPOSITORY / "build-tools"))
import build_executables as build


class PlacesTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def testTheDataOfTheUserIsWhereEachSystemKeepsIt(self):
        with mock.patch.object(sys, "platform", "win32"), mock.patch.dict(os.environ, {"LOCALAPPDATA": "/c/Local"}):
            self.assertEqual(standalone_app.dataHome(), Path("/c/Local/SwarmUP"))
        with mock.patch.object(sys, "platform", "darwin"), mock.patch.object(Path, "home", return_value=Path("/Users/me")):
            self.assertEqual(standalone_app.dataHome(), Path("/Users/me/Library/Application Support/SwarmUP"))

    # An application is replaced when it is updated, and what the user changed stays.
    def testTheRulesAreCopiedOnceAndWhatTheUserChangedStays(self):
        bundled, rules = self.folder / "bundled", self.folder / "rules"
        bundled.mkdir()
        (bundled / "AGENT_RULES.md").write_text("from the application", encoding="utf-8")
        standalone_app.seedRules(bundled, rules)
        self.assertEqual((rules / "AGENT_RULES.md").read_text(encoding="utf-8"), "from the application")
        (rules / "AGENT_RULES.md").write_text("from the user", encoding="utf-8")
        (rules / "MY_RULES.md").write_text("mine", encoding="utf-8")
        (bundled / "NEW_RULES.md").write_text("new", encoding="utf-8")
        standalone_app.seedRules(bundled, rules)
        self.assertEqual((rules / "AGENT_RULES.md").read_text(encoding="utf-8"), "from the user")
        self.assertEqual((rules / "MY_RULES.md").read_text(encoding="utf-8"), "mine")
        self.assertEqual((rules / "NEW_RULES.md").read_text(encoding="utf-8"), "new")

    # The launcher is no interpreter, and pythonw.exe prints nothing: a wheel and pip need a real one. It is in Resources on a Mac, not in Contents.
    def testChildProcessesFindARealPython(self):
        self.assertEqual(standalone_app.findInterpreter(Path("/r/app"), r"C:\r\python\pythonw.exe".replace("\\", "/"), "win32").name, "python.exe")
        with mock.patch.object(standalone_app, "resourceName", lambda name: f"{name}-arm64"):
            self.assertEqual(standalone_app.findInterpreter(Path("/A/SwarmUP.app/Contents/Resources/app"), "/A/SwarmUP.app/Contents/MacOS/SwarmUP", "darwin"),
                             Path("/A/SwarmUP.app/Contents/Resources/python-arm64/bin/python3"))

    # Finder gives only the folders of the system. What a shell prints when it starts is not the path.
    def testThePathComesFromTheLoginShell(self):
        run = lambda *arguments, **options: SimpleNamespace(stdout="Welcome to my shell\nSWARMUP_PATH=/opt/homebrew/bin:/usr/bin\n")
        self.assertEqual(standalone_app.readLoginPath("/bin/zsh", run), "/opt/homebrew/bin:/usr/bin")
        def fails(*arguments, **options):
            raise subprocess.TimeoutExpired("zsh", 5)
        self.assertEqual(standalone_app.readLoginPath("/bin/zsh", fails), "")
        self.assertEqual(standalone_app.mergePaths("/a:/b", "/b:/c", None), os.pathsep.join(("/a", "/b", "/c")) if os.pathsep == ":" else "/a:/b:/b:/c")

    # Each update would leave 160 MB behind on Windows. Another start of this version may still be unpacking.
    def testOnlyTheFoldersOfOtherVersionsAreRemoved(self):
        place = self.folder / "r"
        for name in ("new1", "old1", "old2", "new1.4242"):
            (place / name / "python").mkdir(parents=True)
        standalone_app.removeOldVersions(place / "new1")
        self.assertEqual(sorted(path.name for path in place.iterdir()), ["new1", "new1.4242"])

    def testAProgramThatRunsFromItsSourcesRemovesNothing(self):
        other = self.folder / "sources"
        (other / "another").mkdir(parents=True)
        (other / "mine").mkdir()
        standalone_app.removeOldVersions(other / "mine")
        self.assertTrue((other / "another").exists())

    def testTheFolderOfTheRulesCanBeWithTheDataOfTheUser(self):
        code = "import harness_utils; print(harness_utils.AGENT_RULES)"
        backend = REPOSITORY / "src" / "backend" / "swarm-utils"
        for path in (self.folder / "rules",):
            folders = os.pathsep.join(str(folder) for folder in (REPOSITORY / "src" / "backend").iterdir() if folder.is_dir())
            done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env={**os.environ, "SWARMUP_RULES": str(path), "PYTHONPATH": folders}, cwd=backend)
            self.assertEqual(done.stdout.strip(), str(path), done.stderr)


class WindowTests(unittest.TestCase):
    def fakeServer(self):
        server = mock.Mock(server_port=1234)
        return server

    def runMain(self, webview, browser, window="auto"):
        opened = {"app": None, "tab": None}
        with mock.patch.object(user_interface, "Desk", mock.Mock()), mock.patch.object(user_interface, "keepFresh"), mock.patch.object(user_interface, "makeServer", return_value=self.fakeServer()), \
                mock.patch.object(user_interface, "importWebview", return_value=webview), mock.patch.object(user_interface, "findAppBrowser", return_value=browser), \
                mock.patch.object(user_interface, "openAppWindow", side_effect=lambda found, address: opened.update(app=found)), \
                mock.patch.object(user_interface.webbrowser, "open", side_effect=lambda address: opened.update(tab=address) or (_ for _ in ()).throw(KeyboardInterrupt())), \
                mock.patch.object(sys, "argv", ["user_interface.py", "--window", window]), mock.patch.object(user_interface, "windowFolder", return_value=Path("/tmp")):
            user_interface.main()
        return opened

    def testTheWindowOfTheSystemComesFirst(self):
        webview = mock.Mock()
        opened = self.runMain(webview, "/usr/bin/edge")
        webview.start.assert_called_once()
        self.assertEqual(opened, {"app": None, "tab": None})

    # A computer without WebView2 or .NET must still get its window: this raised, and the program ended.
    def testAComputerWithoutTheEngineGetsAnApplicationWindowOfItsBrowser(self):
        webview = mock.Mock()
        webview.start.side_effect = RuntimeError("no WebView2")
        self.assertEqual(self.runMain(webview, "/usr/bin/edge")["app"], "/usr/bin/edge")

    def testWithoutAnyBrowserWindowItOpensATab(self):
        webview = mock.Mock()
        webview.create_window.side_effect = RuntimeError("no engine")
        self.assertIsNotNone(self.runMain(webview, None)["tab"])

    def testPywebviewNeverFallsBackToTheEngineOfInternetExplorerOnWindows(self):
        webview = mock.Mock()
        with mock.patch.object(user_interface, "windowFolder", return_value=Path("/tmp")), mock.patch.object(user_interface.os, "name", "nt"):
            user_interface.openWebviewWindow(webview, mock.Mock(), "http://x")
        self.assertEqual(webview.start.call_args.kwargs["gui"], "edgechromium")


class FakePip:
    def __init__(self, lines=("Collecting torch\n", "Successfully installed torch\n"), code=0):
        self.stdout, self.code, self.command = list(lines), code, None

    def __call__(self, command, **options):
        self.command, self.options = command, options
        return self

    def wait(self):
        return self.code


class InstallerTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patcher = everywhere(harness_utils, "AGENT_FILES", Path(folder.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: sys.path.remove(str(Path(folder.name) / "packages")) if str(Path(folder.name) / "packages") in sys.path else None)

    def testOnlyWhatTheModelsNeedIsInstalled(self):
        with self.assertRaises(ValueError) as caught:
            package_installer.installPackages(["evil-package"], popen=FakePip())
        self.assertIn("does not install evil-package", str(caught.exception))
        with self.assertRaises(ValueError):
            package_installer.installPackages([], popen=FakePip())

    def testItInstallsInTheDataOfTheUserAndTellsWhatPipSays(self):
        pip, said = FakePip(), []
        self.assertEqual(package_installer.installPackages(["torch"], said.append, pip), ["torch"])
        self.assertEqual(pip.command[1:4], ["-m", "pip", "install"])
        self.assertIn(str(package_installer.packagesFolder()), pip.command)
        self.assertEqual(said, ["Collecting torch", "Successfully installed torch"])

    # In a real test, claude-agent-sdk brought mcp 2, and the application could never start again.
    def testPipKeepsToTheVersionsTheApplicationCarriesAndItsFolderComesLast(self):
        pip = FakePip()
        package_installer.installPackages(["claude-agent-sdk"], popen=pip)
        constraints = Path(pip.command[pip.command.index("--constraint") + 1]).read_text(encoding="utf-8")
        import importlib.metadata
        self.assertIn(f"mcp=={importlib.metadata.version('mcp')}", constraints)
        self.assertEqual(sys.path[-1], str(package_installer.packagesFolder()))

    def testAFailureSaysWhatPipSaid(self):
        with self.assertRaises(ModelError) as caught:
            package_installer.installPackages(["torch"], popen=FakePip(["No space left on device\n"], 1))
        self.assertIn("No space left on device", str(caught.exception))

    def testTorchComesWithItsBuildsForNvidiaOnWindowsOnly(self):
        folder, constraints = Path("/p"), Path("/c")
        self.assertIn(package_installer.TORCH_INDEX, package_installer.installCommand(["torch"], folder, constraints, "win32"))
        self.assertNotIn(package_installer.TORCH_INDEX, package_installer.installCommand(["torch"], folder, constraints, "linux"))
        self.assertNotIn(package_installer.TORCH_INDEX, package_installer.installCommand(["openai"], folder, constraints, "win32"))

    def testTheButtonIsOnlyForTheApplication(self):
        with mock.patch.dict(os.environ, {"SWARMUP_STANDALONE": ""}):
            self.assertFalse(package_installer.canInstall())
        with mock.patch.dict(os.environ, {"SWARMUP_STANDALONE": "1"}):
            self.assertTrue(package_installer.canInstall())


class BuildTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def testTheUniversalProgramHasBothProcessorsAtTheirPlaces(self):
        data = build.makeUniversal([("arm64", b"A" * 100), ("x86_64", b"B" * 50)])
        magic, count = struct.unpack(">II", data[:8])
        self.assertEqual((magic, count), (build.MACHO_FAT, 2))
        first = struct.unpack(">IIIII", data[8:28])
        second = struct.unpack(">IIIII", data[28:48])
        self.assertEqual(data[first[2]:first[2] + first[3]], b"A" * 100)
        self.assertEqual(data[second[2]:second[2] + second[3]], b"B" * 50)
        self.assertEqual((first[2] % 2 ** first[4], second[2] % 2 ** second[4]), (0, 0))
        path = self.folder / "program"
        path.write_bytes(data)
        self.assertEqual(build.machoArchitectures(path), {"arm64", "x86_64"})

    def testABinaryForTheWrongProcessorStopsTheBuild(self):
        (self.folder / "module.so").write_bytes(struct.pack("<Ii", 0xfeedfacf, 0x0100000c) + b"\0" * 32)
        build.checkArchitectures(self.folder, ("arm64",))
        with self.assertRaises(SystemExit):
            build.checkArchitectures(self.folder, ("arm64", "x86_64"))

    def testPackagesThatAreTheSameGoOnceAndTheOthersStayApart(self):
        first, second, shared = self.folder / "a", self.folder / "b", self.folder / "c"
        for folder, code in ((first, b"arm"), (second, b"intel")):
            (folder / "same").mkdir(parents=True)
            (folder / "same" / "x.py").write_text("x = 1", encoding="utf-8")
            (folder / "binary").mkdir()
            (folder / "binary" / "x.so").write_bytes(code)
        build.splitPackages(first, second, shared)
        self.assertTrue((shared / "same" / "x.py").exists())
        self.assertFalse((first / "same").exists() or (second / "same").exists())
        self.assertEqual((first / "binary" / "x.so").read_bytes(), b"arm")
        self.assertEqual((second / "binary" / "x.so").read_bytes(), b"intel")

    def testTheZipKeepsLinksAndWhatCanRun(self):
        folder = self.folder / "app"
        folder.mkdir()
        (folder / "python3.12").write_text("run", encoding="utf-8")
        (folder / "python3.12").chmod(0o755)
        (folder / "python3").symlink_to("python3.12")
        target = self.folder / "app.zip"
        build.zipFolder(folder, target)
        with zipfile.ZipFile(target) as archive:
            link, program = archive.getinfo("python3"), archive.getinfo("python3.12")
            self.assertTrue(stat.S_ISLNK(link.external_attr >> 16))
            self.assertEqual(archive.read(link), b"python3.12")
            self.assertEqual(stat.S_IMODE(program.external_attr >> 16), 0o755)

    def testAFileTooBigForGitHubStopsTheBuild(self):
        path = self.folder / "big"
        with open(path, "wb") as file:
            file.truncate(build.MOST_BYTES)
        with self.assertRaises(SystemExit):
            build.checkSize(path)

    def testTheApplicationsCarryTheVersionsSwarmUpIsTestedWith(self):
        for name in ("windows", "macos"):
            lines = (REPOSITORY / "build-tools" / f"requirements_{name}.txt").read_text(encoding="utf-8").split()
            self.assertIn("mcp==1.28.1", lines)
        self.assertIn('"mcp>=1.28,<2"', (REPOSITORY / "README.md").read_text(encoding="utf-8"))


# The two files themselves, when they are built.
class BuiltFileTests(unittest.TestCase):
    def testTheExeIsAGraphicalProgramWithItsZipBehindIt(self):
        path = OUTPUT / "SwarmUP.exe"
        if not path.exists():
            self.skipTest("SwarmUP.exe is not built.")
        data = path.read_bytes()
        build.checkWindowsProgram(path)
        self.assertEqual(data[-40:-32], b"SWARMUP1")
        offset, size = struct.unpack("<QQ", data[-32:-16])
        self.assertEqual(data[offset:offset + 2], b"PK")
        self.assertEqual(offset + size + 40, len(data))
        self.assertLess(len(data), build.MOST_BYTES)

    def testTheMacApplicationHasWhatMacOsLooksFor(self):
        path = OUTPUT / "SwarmUP.app.zip"
        if not path.exists():
            self.skipTest("SwarmUP.app.zip is not built.")
        self.assertLess(path.stat().st_size, build.MOST_BYTES)
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            for name in ("Contents/Info.plist", "Contents/MacOS/SwarmUP", "Contents/_CodeSignature/CodeResources", "Contents/Resources/SwarmUP.icns",
                         "Contents/Resources/python-arm64/lib/libpython3.12.dylib", "Contents/Resources/python-x86_64/lib/libpython3.12.dylib",
                         "Contents/Resources/app/src/backend/interface/standalone_app.py", "Contents/Resources/app/src/user-interface/index.html", "Contents/Resources/app/agent-rules/AGENT_RULES.md"):
                self.assertIn(f"SwarmUP.app/{name}", names)
            self.assertEqual(stat.S_IMODE(archive.getinfo("SwarmUP.app/Contents/MacOS/SwarmUP").external_attr >> 16), 0o755)
            self.assertEqual(struct.unpack(">I", archive.read("SwarmUP.app/Contents/MacOS/SwarmUP")[:4])[0], build.MACHO_FAT)


if __name__ == "__main__":
    unittest.main()
