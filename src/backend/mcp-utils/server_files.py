# The files server: the agent looks at the files of its folder and of the places of its mission, and changes them (agent_tools.py does the work).
from agent_tools import READ_LINES, onToolbox
from mcp_kit import ServerSpec, ToolSpec


VERSION = "1.0.0"
TEXT = {"type": "string"}
PATH = {"type": "string", "description": "A path in your folder, or in @temp/, @memory/ or @long-term/."}
WHERE = {"type": "string", "description": "A folder in your folder, or @temp, @memory or @long-term (or a folder in them). Empty for your folder."}
REASON = {"type": "string", "description": "Why you do it, in a sentence the user reads."}
CARD = "In your folder, the user approves the change first, and it can be put back. In @temp it needs no approval. In @memory and @long-term the memory rules apply."

TOOLS = [
    ToolSpec("list_files", "List the files and folders of your folder, or of a folder in it, or of @temp, @memory or @long-term, at every depth, with their sizes. "
             "Use it first to see what is there. Use files__find_files to look for names, and files__search_files to look for words.", {"path": WHERE},
             run=onToolbox("list_files"), readOnly=True, idempotent=True),
    ToolSpec("find_files", "Find the files and folders whose name matches a pattern like *.tex, or contains some letters, at every depth. "
             "Use files__search_files to look for words inside the files.", {"pattern": TEXT, "path": WHERE}, ["pattern"],
             run=onToolbox("find_files"), readOnly=True, idempotent=True),
    ToolSpec("search_files", "Find the lines of the text files that contain some words (capitals do not matter), with their file and line number. "
             "Then read the part you need with files__read_file.", {"text": TEXT, "path": WHERE}, ["text"], run=onToolbox("search_files"), readOnly=True, idempotent=True),
    ToolSpec("read_file", "Read a file (text, .docx or .pdf), with the number of each line. A long file is read in parts: start_line and line_count. "
             "Read a file before you change it.",
             {"path": PATH, "start_line": {"type": "integer", "description": "The first line to read, 1 by default."},
              "line_count": {"type": "integer", "description": f"How many lines to read, {READ_LINES} at most."}}, ["path"],
             run=onToolbox("read_file"), readOnly=True, idempotent=True),
    ToolSpec("write_file", "Create a file, or replace all of its text. To change a part of a file that exists, use files__edit_file. " + CARD,
             {"path": PATH, "content": {"type": "string", "description": "The whole text of the file."}, "reason": REASON}, ["path", "content"],
             run=onToolbox("write_file"), destructive=True, idempotent=True),
    ToolSpec("edit_file", "Replace a passage of a text file with another one. Copy old_text exactly from the file (read it first): it must be found once, "
             "unless replace_all is true. " + CARD,
             {"path": PATH, "old_text": TEXT, "new_text": TEXT, "replace_all": {"type": "boolean"}, "reason": REASON}, ["path", "old_text", "new_text"],
             run=onToolbox("edit_file"), destructive=True),
    ToolSpec("make_folder", "Create a folder, with the folders above it. " + CARD, {"path": PATH, "reason": REASON}, ["path"],
             run=onToolbox("make_folder"), idempotent=True),
    ToolSpec("move_path", "Move or rename a file or a folder, also from one place to another. To keep the original, copy it instead. " + CARD,
             {"source": PATH, "target": PATH, "reason": REASON}, ["source", "target"], run=onToolbox("move_path"), destructive=True),
    ToolSpec("delete_path", "Delete a file or a folder with everything in it. " + CARD, {"path": PATH, "reason": REASON}, ["path"],
             run=onToolbox("delete_path"), destructive=True, idempotent=True),
]

SERVER = ServerSpec("files", VERSION, "The files of your folder and of the places of your mission (@temp, @memory, @long-term).", TOOLS)
