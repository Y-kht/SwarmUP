# The transfer server: exact copies of files, folders and blocks of text, also from one place to another (agent_transfer.py does the work).
from agent_tools import hasSession, onToolbox
from mcp_kit import ServerSpec, ToolSpec
from server_files import PATH, REASON, TEXT


VERSION = "1.0.0"
LINE = {"type": "integer"}

TOOLS = [
    ToolSpec("copy_path", "Copy a file or a folder with everything in it, exactly, also from one place to another: for example from your folder to @temp, "
             "to work on a copy or keep a backup, and back. A target that is an existing folder receives the copy inside it. An existing file is replaced "
             "only with overwrite. A copy into your folder waits for the user, and can be put back.",
             {"source": PATH, "target": PATH, "overwrite": {"type": "boolean"}, "reason": REASON}, ["source", "target"],
             run=onToolbox("copy_path"), destructive=True),
    ToolSpec("copy_text", "Copy a block of text into one of your clipboards, exactly: lines start_line to end_line of a file (both included; the whole file "
             "without them), or the text you give. Use it instead of typing a long text again. It changes no file.",
             {"path": PATH, "start_line": LINE, "end_line": LINE, "text": TEXT, "clip": {"type": "string", "description": "The name of the clipboard, main by default."}},
             run=onToolbox("copy_text"), readOnly=True, when=hasSession),
    ToolSpec("paste_text", "Paste a clipboard into a text file, exactly: before the line at_line, after the passage after_text, in place of the passage "
             "replace_text (found once), or at the end (at_end, also to create a file). Give exactly one of them. A paste into your folder waits for the user.",
             {"path": PATH, "clip": TEXT, "at_line": LINE, "after_text": TEXT, "replace_text": TEXT, "at_end": {"type": "boolean"}, "reason": REASON}, ["path"],
             run=onToolbox("paste_text"), destructive=True, when=hasSession),
    ToolSpec("list_clips", "List your clipboards, with their size and their first line.", run=onToolbox("list_clips"), readOnly=True, idempotent=True, when=hasSession),
]

SERVER = ServerSpec("transfer", VERSION, "Exact copies of files, folders and blocks of text.", TOOLS)
