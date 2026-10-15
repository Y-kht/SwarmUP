# The processes server: commands, in the foreground or in the background (background_processes.py does the work).
from agent_tools import hasSession, onToolbox
from background_processes import COMMAND_SECONDS, MAX_COMMAND_SECONDS
from mcp_kit import ServerSpec, ToolSpec
from server_files import REASON, TEXT


VERSION = "1.0.0"
ID = {"type": "string", "description": "The id of the command, like p1."}

TOOLS = [
    ToolSpec("run_command", "Run a command line and read what it printed. It starts in your folder, or in cwd (a folder of yours or of @temp). With background, "
             "it goes on while you work: its output goes to a file in @temp, and processes__check_process and processes__stop_process follow it. The user "
             "approves each command first, and SwarmUP cannot put back what a command changes. Write the scripts you run in @temp.",
             {"command": TEXT, "timeout_seconds": {"type": "integer", "description": f"At most {MAX_COMMAND_SECONDS}, {COMMAND_SECONDS} by default."},
              "cwd": TEXT, "background": {"type": "boolean"}, "reason": REASON}, ["command"],
             run=onToolbox("run_command"), destructive=True, openWorld=True, timeout=MAX_COMMAND_SECONDS + 30),
    ToolSpec("check_process", "See if a command of the background still runs, and the end of what it printed. Without id, all of yours.", {"id": ID},
             run=onToolbox("process_status"), readOnly=True, idempotent=True, when=hasSession),
    ToolSpec("stop_process", "Stop a command of the background, with everything it started.", {"id": ID}, ["id"],
             run=onToolbox("stop_process"), destructive=True, idempotent=True, when=hasSession),
]

SERVER = ServerSpec("processes", VERSION, "Commands, in the foreground or in the background.", TOOLS)
