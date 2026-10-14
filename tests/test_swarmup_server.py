import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

# The modules of SwarmUP are in the folders of src/backend. Their names have hyphens, so they are not packages: each folder goes on the path.
sys.path[:0] = [str(folder) for folder in sorted((Path(__file__).resolve().parent.parent / "src" / "backend").iterdir()) if folder.is_dir() and not folder.name.startswith(("_", "."))]
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


CLI = Path(__file__).resolve().parent.parent / "src" / "backend" / "cli-tool" / "swarmup_cli.py"


# Another AI agent drives SwarmUP through its MCP server (swarmup_cli.py mcp-serve), as a person drives the command line.
class SwarmUpServerTests(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.parameters = StdioServerParameters(command=sys.executable, args=[str(CLI), "mcp-serve"], env={**os.environ, "SWARMUP_HOME": home.name})

    def drive(self, work):
        async def run():
            async with stdio_client(self.parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await work(session)
        return asyncio.run(run())

    def testItsToolsAreTheCommandsOfTheCommandLine(self):
        async def work(session):
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            empty = (await session.call_tool("list_missions", {})).content[0].text
            return tools, empty
        tools, empty = self.drive(work)
        self.assertEqual(set(tools), {"list_missions", "list_sessions", "start_session", "open_mission", "session_status", "read_window", "send_line", "mission_calls",
                                      "close_session", "delete_mission"})
        self.assertTrue(tools["read_window"].annotations.readOnlyHint)
        self.assertTrue(tools["delete_mission"].annotations.destructiveHint)
        self.assertEqual(empty, "The history is empty.")

    def testAnAgentStartsASessionReadsItsQuestionAndAnswersIt(self):
        async def work(session):
            started = (await session.call_tool("start_session", {})).content[0].text
            name = started.split()[1]
            try:
                deadline = time.monotonic() + 20
                text = ""
                while "Who builds the swarm?" not in text and time.monotonic() < deadline:
                    await asyncio.sleep(0.3)
                    text = (await session.call_tool("read_window", {"session": name, "lines": 30})).content[0].text
                await session.call_tool("send_line", {"session": name, "text": "1"})
                deadline = time.monotonic() + 20
                after = text
                while after == text and time.monotonic() < deadline:
                    await asyncio.sleep(0.3)
                    after = (await session.call_tool("read_window", {"session": name, "lines": 30})).content[0].text
                listed = (await session.call_tool("list_sessions", {})).content[0].text
            finally:
                closed = (await session.call_tool("close_session", {"session": name})).content[0].text
            return name, text, after, listed, closed
        name, question, after, listed, closed = self.drive(work)
        self.assertIn("Who builds the swarm?", question)
        self.assertNotEqual(after, question)
        self.assertIn(f"{name}:", listed)
        self.assertEqual(closed, f"Session {name} is closed.")


if __name__ == "__main__":
    unittest.main()
