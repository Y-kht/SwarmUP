import sys
import tempfile
import unittest
from patching import everywhere
from pathlib import Path

# The modules of SwarmUP are in the folders of src/backend. Their names have hyphens, so they are not packages: each folder goes on the path.
sys.path[:0] = [str(folder) for folder in sorted((Path(__file__).resolve().parent.parent / "src" / "backend").iterdir()) if folder.is_dir() and not folder.name.startswith(("_", "."))]
import cli_mcp
import harness_utils
import mcp_external
from base_loop import Loop
from mcp_gateway import Gateway
from mcp_runtime import RUNTIME
from session_mcp import SessionMcp


FIXTURE = str(Path(__file__).resolve().parent / "mcp_fixture_server.py")


class ExternalTestCase(unittest.TestCase):
    def setUp(self):
        files = tempfile.TemporaryDirectory()
        self.addCleanup(files.cleanup)
        self.home = Path(files.name).resolve() / "agent-files"
        self.home.mkdir()
        patcher = everywhere(harness_utils, "AGENT_FILES", self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.gateway = Gateway("m1")
        self.addCleanup(self.gateway.close)

    def add(self, agents="all", approve=True):
        mcp_external.addServer("calc", "stdio", command=sys.executable, args=[FIXTURE], agents=agents)
        tools = RUNTIME.call(mcp_external.inspectServer(mcp_external.loadServers()["calc"]), 60)
        if approve:
            mcp_external.approveServer("calc", tools)
        return tools

    def sneaky(self):
        servers = mcp_external.loadServers()
        servers["calc"]["env"] = {"SNEAKY": "1"}
        mcp_external.saveServers(servers)

    def loop(self, name="Writer"):
        loop = Loop(None)
        loop.name, loop.said = name, []
        loop.notifyUser = loop.said.append
        return loop

    def names(self, loop):
        return [tool["name"] for tool in self.gateway.link(loop).tools() if tool["name"].startswith("calc__")]


class ApprovalTests(ExternalTestCase):
    def testANameCannotTakeTheNameOfATool(self):
        for name in ("files", "skill_email", "a__b", "Calc", "1calc", ""):
            self.assertTrue(mcp_external.checkName(name), name)
        self.assertEqual(mcp_external.checkName("github"), "")
        with self.assertRaises(ValueError):
            mcp_external.addServer("web", "stdio", command="x")
        with self.assertRaises(ValueError):
            mcp_external.addServer("calc", "http", url="ftp://x")

    def testNothingIsGivenBeforeTheUserApproves(self):
        tools = self.add(approve=False)
        self.assertEqual(sorted(tool["name"] for tool in tools), ["add", "hello"])
        self.assertEqual(self.names(self.loop()), [])

    def testAnApprovedServerGivesItsToolsAndItsResultsAreData(self):
        self.add()
        loop = self.loop()
        self.assertEqual(sorted(self.names(loop)), ["calc__add", "calc__hello"])
        text, error = self.gateway.link(loop).call("calc__add", {"a": 2, "b": 3})
        self.assertFalse(error)
        self.assertTrue(text.startswith('<data from="calc">\n5\n</data>'))
        self.assertIn("not instructions", text)
        self.assertEqual(self.gateway.records.read()[-1]["server"], "calc")

    def testOnlyTheAgentsTheUserNamedGetIt(self):
        self.add(agents=["Writer"])
        self.assertTrue(self.names(self.loop("Writer")))
        self.assertEqual(self.names(self.loop("Editor")), [])


class RugPullTests(ExternalTestCase):
    def testAToolThatChangesIsHiddenAndTheUserIsTold(self):
        self.add()
        self.sneaky()
        loop = self.loop()
        self.assertEqual(self.names(loop), ["calc__hello"])
        self.assertIn("The tool add of the MCP server calc changed since you approved it", "\n".join(loop.said))
        text, error = self.gateway.link(loop).call("calc__add", {"a": 1, "b": 1})
        self.assertTrue(error)

    def testTheChangeIsFoundBeforeTheUserApprovesItAgain(self):
        self.add()
        self.sneaky()
        tools = {tool["name"]: tool for tool in RUNTIME.call(mcp_external.inspectServer(mcp_external.loadServers()["calc"]), 60)}
        self.assertTrue(any("instructions to the model" in finding for finding in tools["add"]["findings"]))
        self.assertTrue(any("secret" in finding for finding in tools["add"]["findings"]))
        self.assertEqual(tools["hello"]["findings"], [])

    def testTheWindowApprovesOnlyWhatTheUserSaw(self):
        seen = self.add(approve=False)
        self.sneaky()
        window = SessionMcp()
        with self.assertRaisesRegex(ValueError, "changed while you read them"):
            window.mcpApprove({"name": "calc", "hashes": [tool["hash"] for tool in seen]})
        fresh = window.mcpInspect({"name": "calc"})["inspection"]
        window.mcpApprove({"name": "calc", "hashes": [tool["hash"] for tool in fresh]})
        self.assertTrue(mcp_external.loadServers()["calc"]["approved"])


class CommandLineTests(ExternalTestCase):
    def testTheCommandLineAddsShowsAndApprovesAServer(self):
        parser = __import__("swarmup_cli").makeParser()
        options = parser.parse_args(["mcp", "add", "calc", "--command", sys.executable, "--args", FIXTURE, "--agents", "Writer,Coder"])
        cli_mcp.runMcp(options, ask=lambda question: "y")
        config = mcp_external.loadServers()["calc"]
        self.assertEqual((config["approved"], config["agents"], sorted(config["pins"])), (True, ["Writer", "Coder"], ["add", "hello"]))
        cli_mcp.runMcp(parser.parse_args(["mcp", "remove", "calc"]))
        self.assertEqual(mcp_external.loadServers(), {})


if __name__ == "__main__":
    unittest.main()
