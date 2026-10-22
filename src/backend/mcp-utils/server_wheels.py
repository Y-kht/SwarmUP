# The wheels server (docs/MCP_DESIGN_BOOKLET.md, Table 4.1): the wheels the leader gave an agent for its task, as its tools. A wheel is a
# script the architect wrote from what the agents did again and again (wheel_store.py), so the agent does in one call what took many steps.
# The server has no tool of its own: each agent gets the tools of its own wheels (wheel_tools.py), and nothing else of the long-term memory.
from agent_tools import hasSession
from mcp_kit import ServerSpec
from wheel_tools import provideWheels


SERVER = ServerSpec("wheels", "1.0.0", "The wheels your leader gave you: scripts that do steps agents did by hand before. Use one instead of doing its steps.",
                    [], when=hasSession, provide=provideWheels)
