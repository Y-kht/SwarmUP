# The user server: the agent asks the user a question, through the gateway (elicitation), the same way in the window and in the command line.
from agent_tools import onToolbox
from mcp_kit import ServerSpec, ToolSpec


VERSION = "1.0.0"
# The user can take their time to answer: the time limit of a call waits while the user is asked (AgentContext.waiting).
ANSWER_SECONDS = 600

TOOLS = [
    ToolSpec("ask_user", "Ask the user a question when you cannot go on without the answer. Give options when the answer is a choice. It changes nothing.",
             {"question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}}}, ["question"],
             run=onToolbox("ask_user"), readOnly=True, timeout=ANSWER_SECONDS),
]

SERVER = ServerSpec("user", VERSION, "The user.", TOOLS)
