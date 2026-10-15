# The memory server: the notes of the agent, the memory of its mission and the long-term memory as resources (read only: the files tools
# write them, with the memory rules of mission_memory.py), and the rules files of agent-rules as prompt templates (SKL-08).
from agent_tools import hasSession
from harness_utils import AGENT_RULES, GENERAL_RULES, loadRules, listRules
from mcp_kit import PromptSpec, ResourceSpec, ServerSpec, ToolSpec
from mission_memory import LONG_TERM_INDEX, MISSION_INDEX, describeFile, memoryFiles, readText


VERSION = "1.0.0"


def keepNote(work, note):
    work.loop.session.addNotes(work.loop.name, [note])
    return "Noted. Your notes are shown to you with every request."


def memoryResources(agent):
    session = getattr(agent.loop, "session", None)
    if session is None:
        return []
    memory, found = session.memory, []
    for folder, index, base in ((memory.folder, MISSION_INDEX, f"swarmup://{memory.id}/memory"), (memory.longTerm, LONG_TERM_INDEX, "swarmup://long-term")):
        found.append(ResourceSpec(f"{base}/{index}", index, f"The index of {'the memory of the mission' if index == MISSION_INDEX else 'the long-term memory'}.",
                                  lambda path=folder / index: readText(path)))
        found += [ResourceSpec(f"{base}/{relative}", relative, describeFile(path), lambda path=path: readText(path)) for relative, path in memoryFiles(folder, index)]
    return found


def rulesPrompts(agent):
    files = [{"name": "General", "file": GENERAL_RULES, "about": "The rules every agent follows."}] + listRules()
    return [PromptSpec("rules_" + item["file"][:-len("_RULES.md")].lower() if item["file"] != GENERAL_RULES else "rules_general",
                       f"{item['name']} rules: {item['about']}", lambda file=item["file"]: loadRules(file)) for item in files if (AGENT_RULES / item["file"]).exists()]


TOOLS = [
    ToolSpec("keep_note", "Write down something you must not forget for the rest of the mission: a fact you found, a file that matters, a decision. "
             "Your notes are shown to you with every request, also after a restart.", {"note": {"type": "string"}}, ["note"],
             run=keepNote, readOnly=True, when=hasSession),
]

SERVER = ServerSpec("memory", VERSION, "Your notes, the memory of your mission and the long-term memory, and the rules files.", TOOLS,
                    resources=memoryResources, prompts=rulesPrompts)
