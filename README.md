<div align="center">

<img src="src/user-interface/logo.svg" alt="SwarmUP logo" width="120">

# SwarmUP

**Build and run a swarm of AI agents on your own computer, in its own window or in a terminal.**<br>
You do not need experience with code.

[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](#install)
[![Built on MCP](https://img.shields.io/badge/built%20on-MCP-5B4CF0)](#how-the-agents-work-one-conversation-tools-through-mcp)
[![Windows · macOS · Linux](https://img.shields.io/badge/runs%20on-Windows%20%C2%B7%20macOS%20%C2%B7%20Linux-0E9F8E)](#use-the-interface)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-F4A62A)](LICENSE)

[Install](#install) · [Quick start](#quick-start) · [Features](#features) · [The architect](#the-architect-and-the-wheels) · [Window](#use-the-interface) · [Command line](#use-the-command-line) · [Contributing](#contributing)

</div>

---

## How does it work?

SwarmUP builds and runs a swarm of AI agents for you, on your computer. You use it in its own window or in a terminal.

| Highlight | What it means |
|---|---|
| 🧩 **You build the swarm from parts** | Each agent has its instructions (in your own words, as you would brief an expert), the rules files that it follows, a folder to work in, and its own model. Any model can go with any agent: an API model (GPT, Claude, Gemini, DeepSeek), a free model on your GPUs, or a coding agent (Claude Code, Codex). For work that instructions alone cannot do, there are specialised agents (email, calendar, news, literature review, formatting, math, code). You can also tell a leader agent the mission, and it proposes the full swarm for your approval. |
| 🤝 **The agents work as a team** | They work at the same time, wait for the results of other agents, write to each other, and keep a memory of the mission. The leader manages the swarm while it runs and writes the final report. When a round is over, you give the swarm your next request, as in a chat. |
| ⚙️ **It learns from its own work** | Every swarm has an architect, which works in the background. A miner reads what every agent does and counts what the agents repeat: the same commands, the same errors and how they were fixed, the same sequences of steps, the same reasoning before the same action. The architect turns what repeats into **wheels**, scripts that do those steps in one call. The next swarms get them, and the leader gives each agent only the wheels that its task needs. |
| 🔌 **Every agent works the same way, through MCP** | SwarmUP is built on the Model Context Protocol (MCP). Every agent gets its tools, its data and its skills from MCP servers, through one gateway, whatever its model. So the same tools, the same permissions and the same records apply to every agent. |
| 🧱 **You can extend it** | Add your own rules files to `agent-rules/`. Give your agents the tools of other programs that have an MCP server (GitHub, a database...), after you approve them. Another AI agent can also start and lead your swarms, because SwarmUP is an MCP server too. |
| 🛡️ **You stay in control** | SwarmUP asks you before an agent changes a file, runs a command or reads a web page, and each result waits for your approval. If you reject a result, the files that the agent changed are put back. |
| 💻 **It runs locally** | The window, the swarms, their memory and their history stay on your computer. Only the API models and the web tools that you choose use the internet. For a paid model, you give your API key, and the provider bills you for its use. Some tasks, such as email, also need your credentials. SwarmUP keeps keys, passwords and tokens only in memory while it runs, and never writes them to a file. |

## Install

SwarmUP needs **Python 3.10 or later** and the official MCP library:

```bash
pip install "mcp>=1.28"
```

The other packages are for the parts that you use:

| Part | Install | Notes |
|---|---|---|
| **The window** | `pip install pywebview` | See [Use the interface](#use-the-interface). Without it, SwarmUP opens in a browser. |
| **API models** | `pip install anthropic` (Claude)<br>`pip install openai` (GPT, Gemini, DeepSeek) | SwarmUP reads the key from `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY` or `DEEPSEEK_API_KEY`, or you type it when SwarmUP asks. |
| **Local models** | `pip install torch transformers accelerate`<br>(and `bitsandbytes` for compressed models) | With an NVIDIA GPU (Linux and Windows) or an AMD GPU (Linux). SwarmUP downloads the models to the Hugging Face cache, which is the folder in the `HF_HOME` environment variable. |
| **Coding agents** | `pip install claude-agent-sdk` (Claude Code)<br>`npm install -g @openai/codex@0.160` (Codex) | See [Coding agents](#coding-agents). |
| **PDF files** | `pip install pypdf` | So that the agents can read them. |

SwarmUP tells you which package a model needs before the swarm starts.

- `python tests/live_checks.py gpus` shows what the GPU check finds on your computer.
- `python -m unittest discover -s tests` runs the tests. The tests of the coding agents (`tests/test_coding_agents.py`) run the real Claude Code and Codex with a fake model on your computer, so they need no key and no account. If `claude-agent-sdk` or Codex is not installed, these tests are skipped.

## Quick start

```bash
# In its own window
python src/backend/interface/user_interface.py

# In a terminal: start a session and attach to it
python src/backend/cli-tool/swarmup_cli.py
```

> [!TIP]
> Not sure how to split your mission into agents? Choose **The leader builds the swarm**: give it the mission and a folder, and it proposes the agents, their tasks and their models for your approval.

## Features

### Agents

To build a swarm, you say how many agents you need, the folder of the swarm, the budget of the mission, and what each agent must do, in your own words.

- **An agent does what its instructions say.** Write them as you would brief an expert: what it must make, from what (the files of the folder, the work of other agents), for whom, and in what form. Each agent can follow rules files of `agent-rules/`: you choose them (Author for a text, Coder for code, Literature survey for a review of papers...), or the leader chooses them when it builds the swarm. The general rules (`AGENT_RULES.md`) always apply. You can add your own rules files to `agent-rules/`: they show in the list at the next start.
- **Its work gets better before you see it.** The agent works in a conversation with its tools (it reads the files, and asks your permission to change them). Then a strict review checks its draft against your instructions and its rules, and can read the files to verify it. If the review finds a real problem, the agent writes the draft again with what to fix, two times at most. Then it shows the draft to you. You approve it, reject it or ask for changes, as many times as you need. The result is saved only after your approval, and if you reject it, every file that the agent changed is put back.
- **Specialised agents**, kept apart in a panel that you open when you need one, do one thing that instructions alone cannot. Each one also checks its draft automatically (length, format, its rules, links, missing words, proofs...) and makes it better before you see it. The agent acts only after your approval:

| Agent | What it does |
|---|---|
| ✉️ **Email writer and sender** | It writes in the language that you select, in the tone of your earlier emails. It can read the latest reply from the recipient (IMAP). It sends (SMTP) the exact text that you approved. It has presets for Gmail, Outlook, Yahoo, iCloud and Zoho, and a login test that does not send anything. |
| 📅 **Calendar planner** | It books an event that does not overlap with the other events. It exports `calendar_events.ics`, which you can import into Google Calendar, Outlook and Apple Calendar. |
| 📰 **News briefer** | It reads the feeds of the news outlets that you select, from a list sorted by region and topic, or from any feed address. Then it writes a short briefing with the link of each story, immediately or at the time of day that you select. If you want, it also sends the approved briefing to your phone, on Telegram or WhatsApp, exactly as you approved it. |
| ✍️ **Writer** | It writes a text on a subject, with a maximum length, in the style of your earlier texts. |
| 📚 **Literature reviewer** | It searches Google Scholar, arXiv, Crossref and OpenReview. You can limit the search to some publishers, from a list of the usual publishers or any publisher that it finds by name. It reads the abstracts and writes a survey. It checks each link of the survey against the papers that it found. |
| 📄 **Document formatter** | It changes only the layout of a document, makes sure that no word is missing, and writes a new file. It never changes the original file. |
| ∑ **Math checker** | It reviews a research text one theorem at a time, as a referee does, with quotes. Then a second check tries to prove that each gap or error that it reported is not real. |
| 💻 **Coder** | It writes the code of a file. It runs a command to check the code, but only after you give permission. |
| 🛠️ **Worker** | It does any work that you describe, in its folder, as Claude Code or Codex do: it reads the files, creates, changes, moves or deletes them, and runs commands to check its work, each time after your permission. It ends with a report of what it did. If you do not approve the report, every file that it changed is put back. |

### How the agents work: one conversation, tools through MCP

Every agent works like Claude Code and Codex, whatever its model: an API model (Claude, GPT, Gemini, DeepSeek), a local model, a coding agent, or you, when you lead the swarm. SwarmUP is built on the Model Context Protocol (MCP): it is the MCP host, and every tool, every piece of data and every skill of an agent comes from an MCP server, through one **gateway**.

- **A conversation, not one prompt.** SwarmUP and the model talk turn after turn during the whole run of the agent. The model calls its tools, the gateway runs them and gives back the results, and the model goes on until it answers. When you ask for a change, the change is the next message of the same conversation: the agent still knows what it read and did. The conversation is saved after each step, so a swarm that continues after a stop continues the conversation too.
- **The servers and their tools.** Each tool has the name of its server first. Every tool describes itself: what it does, when to use it, and if you approve it first. Every tool that takes a path also reaches the places of the mission: `@temp/`, `@memory/` and `@long-term/`.

  | Server | Its tools |
  |---|---|
  | `files__` | List, find, search and read the files, with line numbers, also `.docx` and `.pdf`; write, edit, create a folder, move and delete. |
  | `transfer__` | Copy files, folders and blocks of text, and paste them. |
  | `processes__` | Run a command, also in the background. |
  | `web__` | Read a web page. |
  | `memory__` | Keep a note. |
  | `team__` | Write to another agent or to the leader, see the team, read the result of an agent that finished. |
  | `user__` | Ask you a question. |
  | `leader__` | Only for the leader (see [The leader can build the swarm](#the-leader-can-build-the-swarm)). |
  | `wheels__` | The wheels that the leader gave the agent (see [The architect and the wheels](#the-architect-and-the-wheels)). |
  | `skill_<task>__` | The tools of a specialised task, like `skill_email__fetch_latest_email`. |

- **Every model the same way.** The API models use the tool calling of their provider. A local model uses the tools of its chat template, or writes its calls in its text if its template has none. Claude Code and Codex get the tools of SwarmUP as an MCP server of their own (see [Coding agents](#coding-agents)). If you lead the swarm in the command line, you call the tools of the leader with a command (`/leader__add_agent name=...`). A call that cannot be read goes back to the model with the correct format, and after three in a row the leader and you are told. A model that makes the same call a third time in one request does not get it run again: it is told to use the result that it already has. A number or a true/false value written as text (`"1"`, `"true"`) is accepted as such; any other wrong argument goes back to the model with the reason.
- **What an agent sees.** It sees only the tools that it can use now: in plan mode, and while the leader builds the swarm, only the tools that look; the tools of the team only in a swarm; the tools of the leader only for the leader; the tools of a skill only for an agent of that skill. A tool that it was not given is refused, even if the model writes its name. The arguments of each call are checked against the description of the tool before anything runs.
- **Your permission:** reading inside its folder needs none. Changing its temp folder needs none either, and the memory follows its own rules. Each change of a file, command or web page shows you exactly what the agent wants to do (the changes of a file as a comparison), with <kbd>Allow once</kbd>, <kbd>Allow until the next run</kbd>, <kbd>Allow for every agent until the next run</kbd> or <kbd>Deny</kbd>. The same rules apply to every kind of model, coding agents included. If you deny, the agent is told to go on without it or to ask its leader. An address of a web page is checked before you are asked, so you never approve a page that cannot be read.
- **The agents never undo each other's work:** two agents never change the same file at the same time. A file that an agent changed is its own until that agent is done: another agent that wants to change it is told whose it is, and asks that agent or the leader. An agent replaces or deletes a file only as it read it last: a file that it never read, or that changed since (another agent or you changed it), must be read again first. These refusals come before any card, so you are not asked about a change that cannot happen.
- **Corrections never use up an agent:** after its checks, an agent improves its draft a few times on its own before you see it (`numberOfLoops`). Each of your answers starts a new count, so you can correct an agent as many times as you need, and its work is never thrown away because of it.
- **Everything can be put back:** before the first change of a file, SwarmUP keeps a copy of the original in the session. If you remove the agent, stop the swarm, or the agent does not finish, every file that it changed (or created, moved or deleted) is put back. The changes of a command cannot be put back, and the permission card says so.
- **Live:** the card of each agent shows the text it writes while it writes it (Claude and the local models), and what it is doing with its tools. A long call says every few seconds that it still runs. The messages that you or the other agents send reach the agent between two of its steps, never in the middle of a call. When you stop the swarm, every call, every model that writes and every command stops at once.
- **Every call is recorded:** the gateway writes each call of each agent (its time, its tool, its arguments without any secret, how long it took and how it ended) in the memory of the mission. The window shows them with the button <kbd>Calls</kbd>, the command line with `calls`, and the leader can read them. What comes from outside SwarmUP (a web page, an email, another server) is marked as data, and the agents are told never to obey what it says.
- Claude keeps the start of the conversation in the cache of Anthropic, so each step pays the old part at the price of the cache. Each request of an agent has 25 steps at most, and a call has 2 minutes at most by default (the time it waits for you is not counted).

### Folders

After you select the tasks, you can give each agent a folder on your computer to work in. You can also give it no folder. When the leader builds the swarm, all the agents work in the folder of the mission.

- **Every agent reads every file of its folder, at any depth, whatever its model.** SwarmUP shows it the tree of the folder, and it lists a folder, finds files by name, searches words in the files and reads a file with the tools of the files server. SwarmUP reads text files, Word documents (`.docx`) and PDF files (with `pip install pypdf`), in parts when they are long. Reading needs no permission, and nothing outside its folder and the places of its mission can be reached. The conversation shows what each agent reads. Big folders (such as `node_modules`) and hidden files are not listed, but the agent can still look for them. Claude Code also reads the folder with its own tools.
- **Where the results go:** the text, the survey, the briefing, a copy of each email that it sent and the final report of the leader are saved in `swarmup-results/<run>/` inside the folder, for example `swarmup-results/2026-10-07_14-30-05_write-an-article-on-bees/Writer_text.md`. Each run of the swarm has its own folder, named by its time and its mission, and a run that continues after a stop uses the same one. An agent without a folder saves in `agent-files/results/<run>/`. The formatted copy and the math report are saved next to your document, the coder writes its own file, and the calendar file `calendar_events.ics` is in the folder itself.
- **The notes of the agents:** each agent of a swarm can keep notes (a fact that it found, a file that matters, a decision). It reads them with each of its next prompts, also when the swarm continues after a stop, and the notes of plan mode stay for execute mode. They are part of the memory of the mission (see below).
- The files that the formatter, the math checker and the coder work on must be in their folder. The command that checks the code of the coder also starts in this folder.

> [!NOTE]
> **macOS** protects some folders (Desktop, Documents, Downloads, iCloud Drive, external disks). If SwarmUP may not read the folder that you select, it tells you at once. Then allow the app that started SwarmUP (Terminal, iTerm or Visual Studio Code) in **System Settings > Privacy & Security > Files and Folders** (or **Full Disk Access**), and start SwarmUP again. A file that iCloud keeps only in the cloud shows as "in iCloud, not downloaded yet", and SwarmUP asks macOS to download it when an agent reads it.

### The memory of the missions

Each swarm is a **mission**. A mission has rounds: your first request, then each follow-up (see [Missions](#missions-history-follow-ups-several-at-once)). SwarmUP gives every mission a memory, and every swarm a long-term memory, so the swarms get better as you work with them.

| Memory | Where | What is in it |
|---|---|---|
| **The memory of a mission** | `agent-files/mission-specific-memory/<mission>/` | Markdown files about this mission only. SwarmUP writes in it every prompt that you give (`USER_PROMPTS.md`: the mission, the follow-ups, the corrections, the messages to the agents, your answers), where the mission stands (`PROGRESS.md`: the summaries of the leader, the agents that did not finish, the final reports), the plans that you approved (`PLANS.md`), the approved results of every agent (`outputs/WRITER_OUTPUTS.md`, ...) and the notes of every agent (`notes/`). The agents write their own files there too, for the next steps, the next rounds and the other agents. `MISSION.md` is the index, with the writer of each file. |
| **The long-term memory** | `agent-files/long-term-memory/` | Shared by every swarm: your preferences, your styles, the requests that come back, your important notes and the notes about how SwarmUP works for you. Every swarm reads it when it starts, and every agent can read it. Only the leader writes it. After a round where you corrected, rejected, answered or followed up, the leader keeps what the next swarms should know. It does not ask you first, but it always tells you what it changed (the file and the lines), or that it found nothing new to keep. `MEMORY.md` is its index. You can read and change these files yourself at any time. |
| **The temp folder** | `agent-files/mission-temp/<mission>/` | What the agents need only for a while: the scripts that they run, the output of their commands in the background, their notes of the moment, and the copies of files that they work on or keep as a backup. Nothing there is yours, so an agent changes it without asking. The agents are told to put their temporary work there, and never in your folder. |

- **The rules:** every agent reads all the files of its mission. An agent creates files, and it changes or deletes only the files that it wrote. A file of another agent, or of SwarmUP, is theirs. The leader changes every file of its mission. No agent reaches the memory of another mission.
- **What an agent sees:** like Claude Code, an agent gets the index of the memory with its prompt, then the files that matter most to it, within a limit: where the mission stands, your latest prompts, its own notes and results, then the files used most and most lately. It reads the others when it needs them.
- **The places:** an agent writes `@temp/`, `@memory/` or `@long-term/` before a path, in every tool, whatever its model. The memory server also gives the memory of the mission and the long-term memory as MCP resources, and the rules files of `agent-rules/` as prompt templates.
- Several programs can run missions at the same time (sessions of the command line, the window): a file of the memory is always written whole, and a lock of the system keeps two programs from writing the same files together.

### Copy and paste

Every agent copies and pastes as a person does, and exactly: the model never has to type a long text again.

- **Files and folders:** `transfer__copy_path` copies a file or a folder with everything in it, also from one place to another: from your folder to `@temp` (to work on a copy, or to keep a backup), into the memory, and back. A copy is written under another name first and then renamed, so a copy that fails never leaves half a file. A target that exists is only replaced when the agent asks for it, and in your folder its original is kept, so it can be put back. A folder is never copied into itself, links are copied as links, and a copy that is too big is refused. A copy into your folder waits for your permission, with its size.
- **Blocks of text:** `transfer__copy_text` copies lines of a file (or a text) into a clipboard of the agent, with their exact characters and line ends. `transfer__paste_text` pastes it before a line, after a passage, in place of a passage, or at the end of a file. A paste into your folder shows you the change first. Each agent has its own clipboards, by name (`transfer__list_clips`), and they stay when the swarm continues after a stop.
- **Commands in the background:** `processes__run_command` with `background` starts a command that goes on while the agent works, in its folder or in `@temp`. What it prints goes to `@temp/processes/`, and the agent follows it (`processes__check_process`) and stops it (`processes__stop_process`). A command always waits for your permission. The commands of a mission stop when its run ends, when their agent leaves, and when you delete the mission.

### Missions: history, follow-ups, several at once

- **The history:** every swarm that ran stays in the history, with its memory, until you delete it: finished (its round is over), stopped, or interrupted (it can be continued where it stopped).
- **Follow up, as in a chat:** when a round is over, give the swarm a new request. The leader sends each part of it to the agents that it concerns. The other agents sit the round out and keep their result. The leader always works, and reports on the round. The agents keep their conversation, so they still know what they did, also after a restart. You can follow up right away, or later from the history.
- **Several missions at once:** the window shows each open mission in a tab of its sidebar, and the command line runs each one in a session of its own. They run at the same time, and you go from one to another. A tab or a session shows when its mission waits for you.
- **Delete a mission:** its saved state, its memory and its temp folder are deleted, after its commands in the background are stopped. What its agents saved in your folders stays, and so does the long-term memory.

### Messaging apps

The news briefer can send its briefing to Telegram or WhatsApp. The questions about these apps show only for this agent. The program checks the data that you give without sending a message. (On WhatsApp, the only way to check your own number is to send a message to it.) If a briefing is too long, the program sends it in parts.

- **Telegram:** Create a free bot with @BotFather in Telegram, and give its token. Then send a first message to your bot: the program finds your chat number from this message.
- **WhatsApp:** Create a free WhatsApp Business app at developers.facebook.com. Then give its access token, its phone number ID and your own number with its country code. WhatsApp lets such an app send free text only to a person who wrote to it in the last 24 hours. So, you must send a message to the number of the app before the time of the briefing. If you do not, WhatsApp refuses the message, and the program tells you why.

### Swarms

A swarm has one or more agents. The first agent is the leader.

- The agents work at the same time. An agent can wait for other agents and get their results. You select which agents wait, or the leader decides and you approve. If an agent fails, the swarm does not stop, and the leader gets this information.
- The agents talk to each other while they work: an agent can write to another agent, to the leader or to all of them at once, see who does what in the team, and read the result of any agent that finished, also one that it does not wait for. When a sender wrote several messages that an agent did not read yet, the older ones are marked, so an agent that starts late knows which version counts. When an agent writes to the leader, the leader thinks about whether the swarm needs a change, as when you write to it, also in a swarm that you built. The message is shown whole in the swarm (window 0, marked with `!`), so you see it too, and you read it there when you lead the swarm yourself.
- **Plan mode:** Each agent writes the plan of its task. The leader writes a summary of all the plans for your approval. **Execute mode:** Before the agents act, the leader writes a summary of what is done and what will happen next.
- You can approve, reject or correct the summary as a whole, or each agent alone. If you correct the summary, the leader sends the correction to the agents that it is about. If you correct one agent, only that agent changes.
- You can send a message to each agent, the leader included, while it works. The agent reads the message with its next prompt. If the agent did not complete its draft, it writes the draft again.
- **Add and remove agents while the swarm runs.** A new agent joins the agents at work, until the leader starts its final work. It can wait for agents that are already in the swarm, and it gets their results. When you remove an agent, it stops at its next step. SwarmUP restores the files where the agent did not complete its changes, and the model of the agent releases its memory (a GPU, for a local model). If the agent was finished, its result stays, and the agents that need it still get it. All the agents of the swarm get information about who joined or left. Thus, no agent waits for an agent that left.
- The tree of the swarm shows, live, which agent waits for which agent, what needs your approval, and which agents plan, execute or are finished.

### The leader can build the swarm

You can build the swarm one agent at a time, or you can let the leader build it. For the leader, you select its model and the folder of the mission, and you tell it the mission.

- The leader reads the mission, the files of the folder (it can open them), the general agent with the rules files it can choose, and the specialised tasks with their settings. It also reads the models that can run on your computer now: the local models that fit together in your GPUs, the API models (with or without a key), and Claude Code and Codex if they are ready. Then it proposes the full swarm: the agents with their instructions and their rules (or a specialised task when an agent needs one), their models, and which agent waits for which. It gives a reason for each agent.
- You approve the proposal or reject it. You can also tell the leader in your own words what to change, and it writes the proposal again. After your approval, the agents are usual agents of the steps, so you can still change each of them before the run.
- While the swarm runs, SwarmUP asks the leader if the swarm needs a change. It asks each time an agent finishes or someone writes to the leader (in a swarm that you built, only when someone writes to it). The leader can propose to add an agent, to remove an agent (for example, to release a GPU), or to change the model of an agent that did not start. Each proposal has its reason and waits for your approval. If you reject a proposal, SwarmUP never shows it again.
- The leader never controls SwarmUP. It proposes with the tools of the leader server: `leader__propose_swarm`, `leader__add_agent`, `leader__remove_agent`, `leader__change_model`, and `leader__keep_long_term` for the long-term memory. SwarmUP checks each proposal as it checks the forms that you complete: the task, the name, the model (which must fit in the GPUs with the other models), which agent waits for which, and each setting. If something is wrong, the result of the call tells the leader what to correct, before you see it. A proposal while the swarm runs is shown to you at once, and the leader gets your decision as the result of its call. SwarmUP never acts on words that only suggest a change. A small model that writes its proposal as a block (`<swarmup_add>...`) instead of a call is understood too: its adapter reads the block as the call it means, even when its format is not exactly correct.
- SwarmUP never accepts passwords, tokens or API keys from the leader. It asks you for them. It also asks you for the settings that the leader cannot know, for example your own email address.
- At the end, the leader writes the final report of the mission from the results of the agents. After you approve the report, the leader saves it in the folder of the mission.

### The architect and the wheels

Agents spend much of their time and tokens doing again what they did before: finding the test command of a project, reading the same files, reasoning about where the button of a login page is. SwarmUP learns from this. Every swarm has an **architect**, which works in the background for as long as the swarm runs. It has no task of its own, it never takes part in the work of the team, and it never asks you anything.

```text
 agents work ──► journal ──► miner of patterns ──► architect ──► wheels ──► leader gives each agent the wheels it needs
                 (every step)  (counts what repeats)  (writes scripts)            (as tools: wheels__run_unit_tests)
```

- **The journal.** Everything the agents do is written in the journal of the mission (`.swarmup/journal.jsonl` in its memory): every step of a model (what it wrote and the calls it made), every call of a tool with how it ended, the calls that could not run, the permissions, the drafts and their checks, your corrections and those of the leader, the messages and the changes of state. It is written whole, line by line, without secrets. Codex's own commands are in it too.
- **The miner of patterns** reads the journals of a **project** while its swarms run (a project is the folder of the swarm, or the root of its git repository). It turns each step into a shape that keeps what the step is and drops what changes (`python -m pytest <path>.py -q`), and counts the patterns: the same calls and commands, sequences of steps, stuck loops, errors by family (Python, compilers, test runners, package managers, network, git, the refusals of SwarmUP...), how agents got out of an error, the same reasoning (near the same, not only word for word), the same reasoning followed by the same action, files read again and again, the corrections that you, the leader or the checks keep asking for, the permissions you keep giving, the calls a model keeps writing wrong, and the costly steps. For each one it knows how many times, by which agent, in which missions, what it cost, and a few examples. A pattern is new, then emerging, then **repeating** when it came back in several requests. It keeps everything in one SQLite file per project, so it reads only what is new, also after a restart, and very large journals take seconds.
- **The architect** wakes when patterns start to repeat, and once more at the end of each run. It reads a short brief of what repeats and is not automated yet (the best first), reads the patterns it picks with their examples, and writes **wheels**: scripts that do those steps in one call, each with a short summary of its main objective. A wheel is a Python script (`run(args, folder)`) or a short list of calls of the tools of the agent (for the tools of other programs, like a browser). The architect also merges, updates, expands and retires wheels, and fixes a wheel that fails or that an agent had to work around.
- **Only what repeats is automated.** A wheel must cover a pattern that repeats. What does not repeat yet, but could repeat in other projects, gets a short **note** instead, which only the architects read. The architect of another project reads the notes of the architects before it: a pattern that a note describes, and that it sees too, repeats across projects, and gets a wheel. Patterns that repeat a little in several projects count together.
- **The leader gives the wheels.** The wheels are kept apart from the long-term memory (in its hidden folder `.wheels/`), so no agent gets them in its prompt. The leader reads a short list of the wheels of the project, and gives each agent only the wheels its task needs (the field `wheels` of an agent, `leader__give_wheels` while the swarm works, `leader__find_wheels` to search them). An agent gets its wheels as tools (`wheels__run_unit_tests`). When the architect writes a new wheel while agents still work, the leader is told, and can give it at once. In a swarm that you build, you give the wheels yourself.
- **You approve each wheel once.** A wheel is code that a model wrote, so the first time an agent runs a new version of it, SwarmUP shows you its code. Answer <kbd>Always allow this version of the wheel</kbd> (`forever` in the command line) and that version runs without asking from then on, in every swarm. A changed wheel is a new version, and is asked again. Before it is saved, a wheel is checked: nothing runs when it loads, no `eval`, no secrets of yours, no network unless it says so, a summary that only says what it does.
- **The model of the architect** is chosen when you build the swarm: the same as the leader (the default), as another agent, or a model of its own. It spends at most its share of the budget of the mission (10% by default), or $1 in a mission without a budget. A local model that it shares with the leader is only used when the run is over, so the agents never wait for it. A stop of the swarm stops it at once.

> [!TIP]
> See what your agents repeat with `swarmup_cli.py patterns`, read the brief of the architect with `patterns brief`, and the wheels with `wheels list` (see [Use the command line](#use-the-command-line)).

### Your work is never lost

A swarm works in its own thread, and SwarmUP saves its full state after each change.

- If the internet connection stops, the agents that need it pause; they do not fail. The leader tells you that the swarm has no connection. You can continue (SwarmUP tries the failed step again) or cancel. Before you cancel, the leader gives a summary of all the changes that are already done (emails sent, events booked, files written...). It also tells you which tasks will stop before they are complete. Then you select: stop at this point, or continue until the end. If the coder stops, SwarmUP restores the original of its file.
- If the program or the computer stops, the program finds the swarm when it starts again. It tells you that the swarm was interrupted. You can continue the swarm from where it stopped, or cancel it after the same summary. The program does not do again what is already done. It never sends an approved email or message two times. If the program stopped while it sent one, you decide if it sends it again.
- The program never saves passwords, API keys, tokens or accounts. So, it asks for them again when you continue a swarm.

### Models

Each agent has its own model.

| Kind | Models |
|---|---|
| **Local models** | Dozens of open models (Qwen, Llama, Kimi, Mistral, DeepSeek, Gemma, Phi, gpt-oss, GLM, Granite, OLMo, Nemotron), which run on your GPUs. The list shows the VRAM that each model will probably need. |
| **API models** | GPT (OpenAI), Claude (Anthropic), Gemini (Google) and DeepSeek, with their prices. |
| **Coding agents** | Claude Code (Anthropic) and Codex (OpenAI), which run on your computer. They think for an agent as a model does. They can also read files, run commands and use the web, each time with your approval (see [Coding agents](#coding-agents)). |

- Each task has recommended models, and you can also type the name of a different model.
- **First, SwarmUP shows only the models that fit:** The first list of models for an agent shows only the local models that fit in the VRAM that the other agents leave free. It also shows the API models whose price for 1 million tokens fits in the budget that the mission has left. The list of all the models always shows each model, with a mark on the models that do not fit. The leader gets the same lists.
- **One GPU when possible:** a local model goes whole on the GPU with the most free memory when it fits there: it runs faster, and it does not depend on the GPUs passing data to each other. SwarmUP refuses a model that would run in part on the processor, and a model spread over GPUs that do not pass data correctly (a problem of some drivers), with what to do instead. When a worker has finished, its local model lets its memory go, so the leader finds room. Only the GPUs of `CUDA_VISIBLE_DEVICES` are counted.
- **GPU check:** It shows the total VRAM and the free VRAM of your GPUs (NVIDIA on Linux and Windows, AMD on Linux). It also shows the VRAM that the swarm will probably need, which increases with each agent. You cannot select a local model that does not fit. If a model fits but the memory is not free now, you get a warning, and the swarm does not start until the memory is free. The Windows part is written, but nobody has tested it on a real Windows computer yet.

### Budget and cost of the mission

You can give a mission a budget in US dollars for its API models, the leader included. Each API model of the team reserves the price of 1 million of its tokens (the higher price: input or output), as a local model reserves its VRAM. The budget that is left decides which models the first lists show. If an agent spent more than this amount, SwarmUP counts what it spent. When an agent finishes, SwarmUP counts what it really spent. Local models, and Codex with a ChatGPT plan, do not use the budget.

- **SwarmUP counts each call:** the tokens read, read from the cache, written to the cache, and written. It calculates their price as the providers bill them: the cache at its own price, prompts of more than 200,000 tokens at the price for long prompts (for the models that have one), DeepSeek at half price outside its peak hours, and the thinking of Gemini as output. Claude Code gives its own exact cost. If a model has no published price, SwarmUP counts it in tokens and never shows it as free.
- **The full mission counts:** the leader when it builds the swarm, the connection tests, the agents that left and the models that were replaced. SwarmUP saves the cost with the swarm. So, if you continue a mission after a stop, SwarmUP knows what the mission spent.
- **Live:** The cost on the screen increases after each call (in the command line, type `cost`), and it shows what each agent spent. You get a warning at 80% and at 100% of the budget. SwarmUP does not stop anything automatically: you (or the leader, which sees the cost) remove agents or stop the swarm. The prices come from the public list of LiteLLM, so the bill from your provider is always the reference.

### Information from the internet, also offline

SwarmUP keeps the data from the internet that it shows to you in `agent-files/internet-cache`, with the time when it got the data. It updates this data regularly:

- the prices of the models: every hour, in the background while the program runs,
- the models that Codex offers: each time SwarmUP asks Codex,
- the data from Hugging Face about a model, and the publishers found on Crossref: every week.

Without the internet, SwarmUP continues to work with the data of the last connection, also after a restart. It tells you the date of this data: the prices show their date, and "No internet" when they come from the last connection. When the prices change, a call that was already made keeps the price that it was billed at. The data that the agents get for their work (news, papers, messages) is always new: if the connection stops, the agent pauses.

### Settings

The settings of the window (<kbd>Settings</kbd> in the sidebar) are in `agent-files/settings.json`, and they apply to all your missions:

| Setting | Default | What it does |
|---|---|---|
| The most agents a leader can put in a swarm | 10 (1 to 100) | You can always add more agents yourself. |
| The most tools one agent is given | 1000 (no real limit) | An agent with more tools gets the first ones, and you are told. |
| The share of the budget the architect may spend | 10% | In a mission with a budget. |
| What the architect may spend without a budget | $1 | In a mission without a budget. |

### Coding agents

An agent can think with Claude Code or with Codex, instead of a model. The coding agent works in the folder of the agent. If the agent has no folder, it works in an empty folder of its own, in the session of the swarm.

> [!IMPORTANT]
> **SwarmUP asks you before the coding agent does anything other than reading that folder**: a command, a change to a file, a web page, or a file outside its folder. The request shows in the conversation, with the exact command or file. You select <kbd>Allow once</kbd>, <kbd>Allow until the next run</kbd> or <kbd>Deny</kbd>, and you can tell it why. The questions of the coding agent also show there, with their options. The settings files in the folder cannot give a permission in advance, and a swarm that you stopped refuses all new requests.

- **The tools of SwarmUP:** a coding agent runs its own loop, so it gets the tools of its agent as one MCP server, `swarmup`, through a local bridge (only on `127.0.0.1`, with a key of its own: a new one for each turn of Claude Code, and one for each start of Codex). It changes files, copies, runs commands, writes to the team and asks you with these tools, so the same permissions, records and undo apply as for every other agent. Claude Code keeps only its own tools that read (Read, Glob, Grep). A stop of the swarm interrupts the turn of a coding agent.
- **Claude Code** uses your **Anthropic API key**, and you pay for each use, as with the API models. Anthropic does not let other programs use a Claude subscription ([Agent SDK documentation](https://code.claude.com/docs/en/agent-sdk/overview)). So, SwarmUP gives Claude Code its own configuration folder and clears the other credentials. If Claude Code says that it uses something other than the key, SwarmUP stops it. Install its library, which contains Claude Code: `pip install claude-agent-sdk` (Python 3.10 or later).
- **Codex** uses your **ChatGPT plan**. The first time, you sign in from SwarmUP: in your browser, or with a one-time code that SwarmUP shows. (For the code, enable the sign-in with a code in the security settings of ChatGPT.) Codex keeps the sign-in as it does when you use it alone, and SwarmUP never sees your password. Install Codex with `npm install -g @openai/codex@0.160` (it needs Node.js). SwarmUP communicates with Codex through its app-server, which OpenAI still calls experimental. Thus, SwarmUP accepts only the tested version (0.160.x). To try a different version, set `SWARMUP_ALLOW_UNTESTED_CODEX=1`. To give the path of the program, set `SWARMUP_CODEX`. In SwarmUP, the web search of Codex (which would not ask you), its sub-agents and your own MCP servers of Codex are disabled: its only MCP server is the one of SwarmUP. On Windows, SwarmUP asks you about each command of Codex, also the commands that only read.

### MCP servers of other programs

Your agents can also use the tools of other programs that have an MCP server, like a server for GitHub or for a database: a program of your computer (stdio) or a server on the network (Streamable HTTP). Add one in the window (<kbd>Settings</kbd> › <kbd>MCP servers</kbd>) or in the command line (`mcp add`).

- **Nothing is given before you approve it.** SwarmUP connects once, lists the tools of the server, and checks what each tool says about itself: hidden characters, instructions to the model, words about the tools of other servers, questions about a password or a key. It shows you the tools and every warning, and you approve the server, or not.
- **A tool that changes is hidden.** The approval keeps a fingerprint of each tool as you saw it. If the server changes a tool later, the agents do not get it any more, and you are told, until you approve the server again.
- **Its tools are named after it** (`github__create_issue`), so they never take the name of a tool of SwarmUP or of another server. What they give back is marked as data. A server is given to every agent, or to the agents that you name. What a program of your computer writes about itself goes to `agent-files/mcp-logs/`.
- The servers are kept in `agent-files/mcp-servers.json`.

### SwarmUP as an MCP server

Another AI agent can start, lead and follow your swarms through MCP, as you do in the command line. Start the server on stdio:

```bash
python src/backend/cli-tool/swarmup_cli.py mcp-serve

# For example, with Claude Code:
claude mcp add swarmup -- python /path/to/swarm-up/src/backend/cli-tool/swarmup_cli.py mcp-serve
```

Its tools are the commands of the command line: list the missions and the sessions, start or close a session, open or delete a mission, read a window, type a line in a window, see the status of a session and the calls of a mission. The agent answers the questions of a session as a person types them (with the id of the question, so that it never answers another one), so it can do exactly what you can do in the command line, and nothing more: every permission and every approval still waits for an answer in a window.

### Safety

> [!IMPORTANT]
> SwarmUP does nothing without your approval. It keeps API keys, passwords, tokens and accounts only in memory while the program runs. It never writes them to a file, not even in the saved state of a swarm.

## Use the interface

```bash
python src/backend/interface/user_interface.py
```

This opens SwarmUP in its own window, on Windows, macOS and Linux. The interface guides you through six steps. Each step has a guide on the side.

| Step | What you do |
|---|---|
| **1. Mission** | The mission, and who builds the swarm: you, or the leader. The leader shows its proposal in a window, where you approve it, reject it or ask for changes. |
| **2. Agents** | How many agents, and a card for each one, where you write what it must do and choose its rules. The specialised agents are in a panel below the cards: for them you answer the questions of their task, and you can check your email login or your messaging app without sending anything. If you remove an agent, you can use <kbd>Undo</kbd>. |
| **3. Folders** | The folder of the swarm, with the folder dialog of your system. Every agent works in it. An agent can also have a folder of its own (an agent that works on a file somewhere else keeps the folder of its file). |
| **4. Models** | Local models with a gauge of the VRAM of your GPUs, API models with their prices and your key, or a coding agent (Claude Code with your key, or Codex with the sign-in to your ChatGPT plan). The architect is there too: <kbd>Same as …</kbd> an agent (the leader by default), or <kbd>A model of its own</kbd>. |
| **5. Teamwork** | Who waits for whom: you select, or the leader proposes and you approve. When the project has wheels, you also give each agent the wheels it needs. |
| **6. Launch** | Plan first, or execute immediately. |

Then you follow the swarm live on a map (<kbd>Live swarm</kbd>). A golden light shows the agents that wait for you. The leader asks its questions in a conversation on the side, with buttons for the usual answers. You can:

- approve, reject or correct each agent alone,
- send a message to an agent while it works,
- start an agent that waits for its time,
- add an agent or remove an agent (<kbd>Add an agent</kbd>),
- stop the swarm, after the leader tells you what is already done (<kbd>Stop the swarm</kbd>),
- see every call of a tool (<kbd>Calls</kbd>).

If the leader built the swarm, its proposals show in the conversation, with their reason and the buttons to approve or reject them. A card shows what the architect does: its passes, the wheels it made, and its last report. The first time an agent runs a new wheel, its card shows the code, with <kbd>Always allow this version of the wheel</kbd>. When a round is over, a box under the map takes your next request (follow up), in plan mode or right away (<kbd>Send to the swarm</kbd>).

- **Your missions:** the sidebar has a tab for each open mission, with a dot that shows when it runs or waits for you. <kbd>New mission</kbd> opens another tab, and the missions of the other tabs go on. The home page shows the history of the missions: continue an interrupted one (you give the passwords and keys again), cancel it, follow up a finished one, or delete one with its memory and its temp folder.
- **The window itself:** install pywebview: `pip install pywebview`. On Linux, it also needs GTK or Qt: `pip install pywebview[gtk]` or `pip install pywebview[qt]`. It uses the web engine of the system: WebView2 on Windows (already installed on Windows 10 and 11) and WebKit on macOS. Without pywebview, SwarmUP opens in an application window of Edge, Chrome, Chromium or Brave, if one of them is installed. If not, it opens in a tab of your web browser. To select the window yourself, use `--window browser` (or `app`, `webview`, `none`).
- The interface accepts connections only from your own computer (127.0.0.1), and only the window that it opened can use it. SwarmUP never sends passwords, keys or tokens back to the window, and never writes them to a file.
- When you close the window, the program stops. The swarms that run are saved first, so you can continue them at the next start: they are in the history.

## Use the command line

SwarmUP also works in a terminal, as the window does. A swarm runs in a **session**: a program in the background that goes on when you close the terminal. You come back to it from any terminal, and you close it when you want.

```bash
python src/backend/cli-tool/swarmup_cli.py            # start a session and attach to it (the same as: new)
python src/backend/cli-tool/swarmup_cli.py list       # the sessions that run, and if one waits for you
python src/backend/cli-tool/swarmup_cli.py attach s1  # come back to session s1 (without a name: the latest session)
python src/backend/cli-tool/swarmup_cli.py kill s1    # close session s1 (--all closes them all)
```

#### Sessions and missions

| Command | What it does |
|---|---|
| `new [--detached]` | Starts a session and attaches to it. With `--detached`, it only starts it. |
| `attach [session]` | Attaches to a session: `s1`, `1`, or the latest one. |
| `list` (or `ls`) | Lists the sessions, with their state (building, running, waiting for you, finished), their agents and their mission. |
| `status [session] [--json]` | Shows where a session stands (the tree, the cost, the question that waits), without attaching. |
| `show session [window] [--lines N]` | Shows the latest lines of a window, without attaching. |
| `send session text [--window W] [--question N]` | Types a line in a window of a session (window 0 by default), without attaching: an answer, a command or a message, for scripts. With `--question`, the line answers question N (`status` shows the id of the question that waits), or is refused. |
| `kill [session] [--all]` | Closes a session. A swarm that runs is saved, and the next start offers to continue it. |
| `run` | Runs SwarmUP in this terminal only, without a session. It stops when the terminal closes. |
| `history [--json]` | Lists every mission of the history, the latest first, with its state, its rounds and the session it is open in. |
| `open mission [--detached]` | Opens a mission of the history (its number in `history`, or its id) in a new session: an interrupted mission goes on where it stopped, and a finished one takes your next request. |
| `delete mission... [--yes]` | Deletes missions of the history, with their memory and their temp folder (after a confirmation). |
| `calls mission [--agent A] [--server S] [--lines N]` | Shows every call of a tool in a mission: its time, agent, tool and arguments, how long it took and how it ended, with the counts of each agent. |

#### MCP

| Command | What it does |
|---|---|
| `mcp list` | Lists the MCP servers of other programs, approved or not, and the agents they are given to. |
| `mcp add NAME --command CMD [--args "..."] [--env K=V] [--agents A,B]` | Adds a server of this computer (or `--url URL [--header K=V]` for one on the network), then shows its tools and asks you to approve it (`--yes` approves without asking: use it only for a server whose tools you already read). |
| `mcp approve NAME` / `mcp remove NAME` | Approves a server again, as it is now, after you read its tools and the warnings; or removes it. |
| `mcp-serve` | Runs SwarmUP as an MCP server on stdio, so another AI agent can drive it (see [SwarmUP as an MCP server](#swarmup-as-an-mcp-server)). |

#### Patterns and wheels

| Command | What it does |
|---|---|
| `patterns [project] [--kind K] [--status S] [--top N] [--json]` | Lists the patterns the agents of a project repeat, the best first (the project is its id, its name or its folder; the current folder by default). |
| `patterns brief [project]` / `patterns show ID` / `patterns search TEXT` | What the architect reads at the start of a pass; one pattern with examples from the journals; the entries of the journals that contain a text. |
| `patterns mine [project] [--rebuild]` / `patterns projects` | Reads the journals of a project now (`--rebuild` reads everything again, the missions made before the journal too); lists the projects. |
| `wheels [list] [--project P]` / `wheels show NAME` | Lists the wheels, with their versions and whether you approved them always; shows the code of one. |
| `wheels approve NAME` / `wheels revoke NAME` / `wheels retire NAME [--why W]` / `wheels notes` | Approves the version of a wheel always, takes the approvals back, retires a wheel, or lists the notes of the architects. |

`new` and `attach` accept `--plain` (lines instead of windows) and `--prefix C-a` (another prefix key, for example inside tmux).

#### The windows

A session works like tmux. **Window 0** is the swarm: its tree at the top, everything that happens below, and every question for you. **Each agent has its own window**: its state at the top, then what it does with its tools, what it says, its messages, its draft when it waits for you, and its result. The status bar at the bottom lists the windows: `*` is the window on the screen, `!` a window whose question waits for you, `+` a window with something new, and `-` an agent that left. It also shows the cost and the state of the swarm.

| Keys | What they do |
|---|---|
| <kbd>Ctrl</kbd>+<kbd>b</kbd> then <kbd>0</kbd>…<kbd>9</kbd> | Go to a window. |
| <kbd>Ctrl</kbd>+<kbd>b</kbd> then <kbd>n</kbd> / <kbd>p</kbd> | The next or the previous window. |
| <kbd>Ctrl</kbd>+<kbd>b</kbd> then <kbd>w</kbd> / <kbd>'</kbd> | Choose a window from a list / by its name. |
| <kbd>Ctrl</kbd>+<kbd>b</kbd> then <kbd>d</kbd> | Detach: the session goes on. |
| <kbd>Ctrl</kbd>+<kbd>b</kbd> then <kbd>x</kbd> | Close the session. |
| <kbd>Ctrl</kbd>+<kbd>b</kbd> then <kbd>?</kbd> | Help. |
| <kbd>PgUp</kbd> / <kbd>PgDn</kbd> | Scroll. |
| <kbd>↑</kbd> / <kbd>↓</kbd> | Bring back what you typed. |
| <kbd>Esc</kbd> | Clear the line. |

You can also type `:w 2`, `:next`, `:prev`, `:detach`, `:kill` or `:help`.

#### Commands while the swarm runs

In window 0 you answer the questions and type the commands of the swarm (`help` lists them). In the window of an agent, what you type is about that agent: `approve`, `reject`, `correct <what to change>`, `start`, `remove <why>`, or any other text, which is sent to it as a message. A question is answered in its window or in window 0.

| Command | What it does |
|---|---|
| `approve Writer` / `reject Writer` / `correct Writer shorter` | Decide about the draft of an agent. |
| `msg Writer ...` (or `Writer: ...`) | Send a message to an agent. |
| `start Writer` | Start an agent that waits for its time. |
| `add` / `remove Writer <why>` | Add an agent while the swarm runs, or remove one. |
| `wheels` / `wheels Writer run_unit_tests` | List the wheels of the project, or give them to an agent while the swarm runs. |
| `tree` / `log` / `cost` | The tree of the swarm, the latest messages, what the mission spent. |
| `stop` | Stops the swarm for good: the leader first lists what the swarm already did, and you confirm. |
| `quit` (or `kill`) | Closes the session and saves the swarm, so you can continue it later. |

- A command for an agent (`approve Writer`, `correct Writer shorter`, `msg Writer ...`, `remove Writer ...`) and `cost`, `add` and `stop` always run as commands, also while a question waits, so they are never taken as its answer. A question is withdrawn when it is not needed anymore (for example, the summary of the leader once you decided each agent alone). The questions of `add` come one after the other, and no other question comes between them.
- When a round is over, window 0 asks for your next request (follow up), or ends. A session stays open after its swarm is finished, so you can read the results. Close it with <kbd>Ctrl</kbd>+<kbd>b</kbd> <kbd>x</kbd> or `kill`: the mission stays in the history.
- Each session has its files in `agent-files/cli-sessions/`: the address of the session (only you can read it) and its log. A session only accepts connections from your computer, with its own key. The folder `agent-files` can be moved with the `SWARMUP_HOME` environment variable.
- The windows need curses. It is part of Python on Linux and macOS. On Windows, install it with `pip install windows-curses`, or use `--plain`.

#### The questions of a session

The questions of a session guide you as the window does:

1. who builds the swarm: you, or the leader, whose proposal you approve,
2. how many agents the swarm has,
3. what each agent must do, in your own words, and the rules that it follows (type `tasks` instead for a specialised task: email, calendar, news, papers, documents, proofs, code, files),
4. the folder of the swarm (and, if you want, a folder of its own for some agents),
5. the model of each agent: a local model on your GPUs (with the VRAM that it needs and a check of your GPUs), a paid model through an API (with the prices), or a coding agent (Claude Code or Codex, which ask you before they act). From the second agent on, the model of the agent before is offered first, so a swarm of agents with one model needs one answer for each,
6. the model of the architect: the same as the leader (or another agent), or a model of its own, and, when the project has wheels, the wheels of each agent.

> [!TIP]
> **You can lead the swarm yourself:** for the first agent, choose "Me" instead of a model. What the swarm asks its leader (its own task, which agents a follow-up concerns, what to keep in the long-term memory) is then asked to you in the window of the leader, and your answer is the answer of the leader. The leader does not summarise for you or check its own work. This is useful when the user of the command line is itself an AI agent, which answers with `send` (one line; write `\n` for a new line). Such an agent should give the id of the question it answers (`status` shows it): `send s1 --question 154 yes` is refused if another question waits by then, so it never answers the wrong one. You see only what is new, and you call the tools of the leader with a command: `/leader__add_agent name=Translator task=agent model=claude-haiku-4-5 why="..."`, or `/name {JSON}`. Such a mission is continued from the command line only.

- SwarmUP does nothing before you approve it. It sends emails, books events and writes files only after you approve the exact result.
- If the connection stops, the swarm pauses and asks you to type `continue` or `cancel`. If the program or the computer stops, start the program again. It lets you continue the swarm that was interrupted, or cancel it after a summary of what it did.

## For developers

<details>
<summary><b>Repo structure</b></summary>

<br>

```text
swarm-up/
├── agent-rules/         the rules files of the agents (and ARCHITECT.md for the architect)
├── agent-files/         the state, the memory and the history of the missions (made when SwarmUP runs)
├── docs/                CONTRIBUTING.md, CODE_OF_CONDUCT.md
├── src/
│   ├── backend/
│   │   ├── architect-utils/   the journal, the miner of patterns, the wheels and the architect
│   │   ├── cli-tool/          the command line
│   │   ├── interface/         the window
│   │   ├── mcp-utils/         the MCP layer: the gateway and the servers
│   │   ├── model-utils/       the models, their adapters and the prompts
│   │   ├── swarm-utils/       the swarm, its agents, its memory and its leader
│   │   └── task-utils/        the tasks and their lists
│   └── user-interface/  the pages of the window
└── tests/               one test file for each part
```

- **`LICENSE`**: SwarmUP is licensed under the Apache License 2.0.
- **`docs/`**: The documentation of the project.
- **`src/`**: The source code of the project: the user interface, the logic of the agents and other core utilities.
  - `src/backend/interface/`: the interface.
    - `user_interface.py` runs the interface in its own window, and `web_server.py` is the local server of the window.
    - `interface_views.py` prepares what the window shows.
    - `session_core.py` holds the `Session`, which connects one mission of the window to its swarm. Its parts are the steps of the form (`session_steps.py`), the models (`session_models.py`), the swarm that runs (`session_runs.py`) and the missions of the history that are continued or followed up (`session_saved.py`).
    - `session_desk.py` holds the `Desk`: the missions that are open at the same time, one `Session` in each tab, with the history of the missions.
    - `session_mcp.py` shows the MCP layer in the window: the calls of the tools of a mission, and the MCP servers of other programs. `session_architect.py` has the architect of the window: its model, its card, and the wheels of each agent.
  - `src/backend/mcp-utils/`: the MCP layer. Every agent reaches its tools, its data and its skills through it, whatever its model (see [How the agents work](#how-the-agents-work-one-conversation-tools-through-mcp)).
    - `mcp_gateway.py` holds the `Gateway` of a mission and the `AgentLink` of each agent: one MCP session with each server, the tools the agent can use now, and the fixed sequence of every call (the checks, the permission, the time limit, the record, the events). `mcp_runtime.py` runs the MCP sessions in one event loop of their own.
    - `mcp_kit.py` describes a server and its tools, resources and prompt templates (`ServerSpec`, `ToolSpec`). `mcp_policy.py` is the one permission policy for every agent. `mcp_records.py` records every call, without secrets. `path_guard.py` keeps the agents from undoing each other's work on your files.
    - The servers of SwarmUP: `server_files.py`, `server_transfer.py`, `server_processes.py`, `server_web.py`, `server_memory.py`, `server_team.py`, `server_user.py` and `server_leader.py`, the skills in `skill_servers.py`, the wheels of each agent in `server_wheels.py`, and the tools of the architect in `server_architect.py`.
    - `mcp_external.py` holds the MCP servers of other programs that you add, and `mcp_bridge.py` gives the tools of SwarmUP to Claude Code and Codex.
  - `src/backend/architect-utils/`: the architect and the wheels (see [The architect and the wheels](#the-architect-and-the-wheels)).
    - `agent_journal.py` writes the journal of each mission, and `project_identity.py` finds the project of a swarm.
    - The miner of patterns: `pattern_miner.py` (the engine), `pattern_shapes.py` (the shapes of the steps: commands, paths, addresses, arguments), `pattern_errors.py` (the families of errors), `pattern_text.py` (the reasoning that comes back, near the same), `pattern_sequences.py` (sequences, loops and fixes), `pattern_detectors.py` (from the journal to patterns), `pattern_store.py` (the store of a project, in SQLite), `pattern_tail.py`, `pattern_legacy.py` (the missions made before the journal), `pattern_registry.py` (what the architects of all projects share: the registry and the notes) and `pattern_digest.py` (what the architect and you read). `miner_service.py` runs a miner in the background for each project.
    - The wheels: `wheel_store.py` (versions, aliases, approvals), `wheel_checks.py` (what a wheel must be before it is saved), `wheel_tools.py` (a wheel as a tool) and `wheel_runner.py` (runs a script wheel in a process of its own).
    - The architect: `architect_loop.py` (its loop, which never asks you anything, and the choice of its model) and `architect_manager.py` (its work in the background, its passes and its budget).
  - `src/backend/swarm-utils/`:
    - The tools that all parts use: `harness_utils.py` (the context files, the internet and the feeds), `agent_storehouse.py` (the files of the folders, and where the results are saved), `agent_conversation.py` (the core loop of each agent: its conversation with its model, and its calls through the gateway), `agent_tools.py` (the work behind the tools of the files, their places, and how their changes are put back), `agent_transfer.py` (copy and paste of files, folders and blocks of text), `background_processes.py` (the commands, also in the background), `internet_cache.py` (the data from the internet, kept for when there is no connection), `mission_costs.py`, `user_settings.py`, `gpu_check.py`, `messengers.py` (the messaging apps) and `saved_swarms.py`.
    - The loops of the agents: `base_loop.py` (the `Loop` that all loops are based on), `general_loop.py` (the agent that does what its instructions say, with its review), `message_loops.py` (email, calendar and news), `writing_loops.py` (writing, papers, literature review and formatting) and `checking_loops.py` (math, code and the worker).
    - The swarm: `swarm_harness.py` holds the `Swarm`. Its members are in `swarm_team.py`, its reviews are in `swarm_review.py`, its run is in `swarm_run.py` and its rounds (the follow-ups of the user) are in `swarm_rounds.py`.
    - The missions: `mission_memory.py` (the memory of each mission, the long-term memory and the temp folder, with their rules), `memory_cache.py` (what each agent sees of the memory with its prompt) and `mission_history.py` (the history of the missions: list, open, delete).
    - The leader, which builds and manages the swarm: `leader_utils.py` (`designSwarm`), `leader_catalog.py`, `leader_checks.py`, `leader_manager.py`, `leader_wheels.py` (the wheels the leader gives to the agents), and `leader_parser.py` (the proposals that a model writes as blocks instead of calls).
  - `src/backend/model-utils/`:
    - The lists of models (`models_library.py`) and the prompts (`agent_prompts.py`, with the prompts of the leader in `leader_prompts.py` and those of the architect in `architect_prompts.py`).
    - The clients of the models (`model_clients.py`), with their shared parts in `model_support.py` and the words of each provider in `model_messages.py`. `model_adapters.py` gives every kind of model the same interface.
    - The coding agents: `coding_agents.py` for Claude Code and `codex_agent.py` for Codex.
  - `src/backend/task-utils/`: the tasks and their questions (`tasks_library.py`), and the lists that you select from (`sources_library.py`).
  - `src/backend/cli-tool/`: the command line. `swarmup_cli.py` has the commands and the program. The questions are in `cli_console.py`, `cli_steps.py` (tasks and folders), `cli_models.py` and `cli_program.py` (the swarm and its run), and `cli_resume.py` continues an interrupted mission or follows up a mission of the history. `cli_mcp.py` has the commands of the MCP servers and of the calls, `cli_patterns.py` the commands of the patterns and of the wheels, and `swarmup_mcp_server.py` is SwarmUP as an MCP server. `cli_view.py` shows the swarm and runs the commands of the user. A session is made of `cli_sessions.py` (start, list and close), `cli_hub.py` (the daemon: its windows and its connections) and `cli_screen.py` (the windows in the terminal).
  - `src/user-interface/`: the pages of the interface (HTML, CSS, JavaScript and SVG icons). `index.html` loads the scripts in sequence, from `page_tools.js` to `app.js`. `missions.js` has the tabs of the missions, the history and the follow-up, `mcp_views.js` the calls of a mission and the MCP servers, and `architect_view.js` the architect and the wheels. It loads the styles from `style.css` and `views.css`.
  - The names of the folders in `src/backend/` have hyphens, so these folders are not Python packages. Thus, the programs and the tests add each folder to the Python path.
- **`tests/`**: One test file for each part. `patching.py` has `everywhere`. For a test, this function replaces a function in all the modules that come from the same original file.
- **`agent-files/`**: The files for the functions of the agents, such as context files, memory files and other related resources. SwarmUP saves the state of each mission in `agent-files/swarm-runs/` (the history), the memory of each mission in `agent-files/mission-specific-memory/`, the long-term memory of all the missions in `agent-files/long-term-memory/` (with the wheels in its hidden folder `.wheels/`, and what only the architects read in `.architect/`), the temp folder of each mission in `agent-files/mission-temp/`, and the results of the agents that have no folder in `agent-files/results/`.
- **`agent-rules/`**: The rules and configurations for the behavior and the decisions of the agents. Each agent must use these rules to make sure that it stays within the defined parameters and guidelines. `AGENT_RULES.md` has the general rules of every agent that follows instructions, and you (or the leader) choose the other files that apply to each agent. Each specialised agent has its own rules file. A file that you add here (`NAME_RULES.md`) can be chosen too. `ARCHITECT.md` has your rules for the architect.

</details>

<details>
<summary><b>The parts of SwarmUP, for your own programs</b></summary>

<br>

`Swarm.addListener` gives each change of a swarm as an event. `tasks_library.py` describes the questions of each task, and `models_library.py` holds the lists of models. The graphical interface (`src/backend/interface/user_interface.py`) uses these parts. Its `Session` (`session_core.py`) connects each loop to the window: `notifyUser`, `askUser` and `askSecret` become messages and questions in the conversation. The window asks the session for news with `/api/poll`.

- `Swarm.startInBackground` runs a swarm in its own thread. Use `isRunning` and `wait` to monitor it. `findUnfinishedSwarms` of `saved_swarms.py` finds the swarms that were interrupted, and `Swarm.restore` restores one of them. The interface builds its agents again, and `publicAnswers` and `restoreAnswers` of `tasks_library.py` keep the secrets out of the saved data. Then `resume` continues from where the swarm stopped.
- After the connection is lost (the events `connectionLost`, `resumed` and `stopped`), an interface answers with `continueWork`, `summarizeChanges` and `stopWork`, as the console does.
- `sources_library.py` lists the messaging apps (`MESSAGING_APPS`), and `messengers.py` has the code that sends to each app.
- A coding agent (`ClaudeCodeModel` in `coding_agents.py`, `CodexModel` in `codex_agent.py`) gets the tools of SwarmUP through `BRIDGE.offer` (`mcp_bridge.py`), and what it asks with its own tools goes to `mcp_policy.decidePermission`, which shows `Loop.askPermission` and `Loop.askQuestions`. An interface replaces these functions, as it replaces `askUser` (the console and the window both do this).
- `Swarm.addAgent` and `Swarm.removeAgent` work while the swarm runs (the events `joined`, `removed` and `retired`). `Swarm.setModel` works for an agent that did not start (the event `model`).
- `agent_conversation.py` holds the core loop of an agent (`Conversation`, `talk`): every model is reached through its adapter (`adapterOf` in `model_adapters.py`), which gives `converse(system, messages, tools)` and `{text, calls, raw, cut}`. `ApiModel` and `LocalModel` translate the messages and the tools into those of their provider (`claudeMessages`, `responsesInput` for GPT through the Responses API of OpenAI, `openAiMessages` for Gemini and DeepSeek through Chat Completions, `localMessages`, and `readToolCalls`, which reads the calls that a local model writes in its text, in `model_messages.py`); a client with only `input(prompt)` gets `PromptAdapter`. An adapter can also say what it can do: `streams` (it gives its text while it writes, `onText`), `runsTools` (it runs its tools itself, with `runTool`: the coding agents), `canShorten`, `approvesItself` (the user who leads), and `cancel()`. The calls go to the gateway through `AgentLink.call(name, arguments, readOnly)`.
- The gateway (`mcp_gateway.py`): `Gateway(missionId, folder, onEvent)` holds the links of the agents of a mission (`link(loop)`, `drop`, `cancelAll`, `claimPath`), its `guard` (`PathGuard` in `path_guard.py`: `claim`, `own` and `release`, and `note` and `checkKnown` for what each agent read last; the swarm releases the files of an agent that is done, failed or left), and its `records` (`CallRecords` in `mcp_records.py`). A server is a `ServerSpec` (`mcp_kit.py`) whose tools are `ToolSpec`s run as `run(work, **arguments)`: `Work.permit`, `Work.ask` and `Work.propose` go to the user through MCP elicitation, and `mcp_policy.decidePermission` decides. `agent_tools.py` has the work behind the tools of the files (`Toolbox`), the permissions of the run (`Loop.allowed`, cleared at each run) and `undoChanges`, which `Loop.rollback` calls. The swarm gives each loop `team` (`relay`, `describeTeamFor`, `resultOf`, `shouldStop`), `gateway`, `onActivity` and `onText` (the events `activity` and `text`).
- `MissionMemory` in `mission_memory.py` is the memory of a mission: `record`, `addUserPrompt`, `addProgress`, `addPlan`, `addOutput`, `addNotes`, `readRound`, and `write` and `delete`, which follow the rules of the owners (`refusal`, `ruleFor` for the coding agents). The long-term memory calls `onLongTermChange` after each change, which the swarm turns into a message to the user; the leader writes it with `leader__keep_long_term`. `describeMemory` in `memory_cache.py` writes what an agent sees of it. `WorkSession` is the memory of a swarm while it runs.
- `listMissions`, `loadMission` and `deleteMission` of `mission_history.py` are the history. `Swarm.followUp(request, mode)` starts the next round (`swarm_rounds.py`), and `run` runs it. The `Desk` of `session_desk.py` holds the tabs of the window, and the browser names its tab with the header `X-SwarmUP-Tab`.
- The places of the tools (`@temp`, `@memory`, `@long-term`) are resolved by `splitPlace` of `agent_storehouse.py` and `Toolbox.locate` and `Toolbox.target` of `agent_tools.py`. `TransferTools` (`agent_transfer.py`) and `ProcessTools` (`background_processes.py`) are parts of the `Toolbox`, and the servers (`server_*.py`) describe their tools. A window gives each `Session` its own `Loop.userLock`, so the questions of one mission never wait for the answers of another.
- `agent_storehouse.py` has the file system of the agents: `describeTree`, `findFiles`, `searchFiles`, `extractText`. `Loop.askAgent(prompt, own, tools)` runs the core loop; the calls that only check a draft pass `tools=False` and get no tool. `Swarm.prepareRun` makes the `WorkSession` of the swarm (its notes and workspaces) and the name of the run (`runName`, saved with the swarm), and `Loop.saveResult` writes in `resultsFolder`. `folderProblem` tells why a folder cannot be read.
- `remember` in `internet_cache.py` keeps the data that the program gets from the internet, with its age limit, and the last version for when there is no internet. `describeCached` tells the date of this data, and `keepFresh` updates the prices in the background.
- `MissionCosts` in `mission_costs.py` counts what a mission spends, with its budget. Each client records its calls with `recordCall` of `model_support.py`. `MissionCosts` has `report` for the screen, `left` for the decisions, and `warnings` at 80% and 100%. The loop calls `Loop.onUsage` after each call of a model, so an interface can show the cost as it increases. `loadSettings` and `saveSettings` of `user_settings.py` keep the settings of the user.
- The architect and the wheels: the swarm writes its `Journal` (`agent_journal.py`; `Loop.note(kind, **fields)` adds an entry, and the gateway writes each call), and `projectOf` (`project_identity.py`) finds its project. `MINERS.acquire(project, user)` (`miner_service.py`) runs the `Miner` of a project (`pattern_miner.py`: `poll`, `rebuild`) in the background, and tells its subscribers of new signals. `attachArchitect(swarm, choice, makeClient, say)` (`architect_manager.py`) gives a swarm its `ArchitectManager`, which runs its passes on an `ArchitectLoop`. A server can give each agent tools of its own with `ServerSpec(provide=...)` (the wheels server does), and a loop can be narrowed to some servers and tools with `Loop.toolScope` (the architect is). The wheels are in `wheel_store.py` (`saveWheel`, `resolve`, `approve`), checked by `checkWheel` (`wheel_checks.py`); a request of a permission with a `pin` can be approved always (`mcp_policy.py`).
- The leader that builds the swarm:
  - The tools of the leader are in `server_leader.py`, with its desk (`LeaderDesk`: building the swarm, or managing it). `leader_parser.py` has `parseOutput` and `readBlockCalls`, which read the blocks a model writes instead of calls.
  - `LeaderCatalog` gives the leader its information (in `leader_catalog.py`) and checks its proposals (in `leader_checks.py`).
  - `designSwarm` (in `leader_utils.py`) builds the swarm with the user.
  - `LeaderManager` (in `leader_manager.py`) is the leader while the swarm runs: `propose` checks a change, asks the user and makes it.
  - The leader asks the user through `Loop.askProposal`. The program that runs the swarm makes the agents for the leader (`makeLoop`, `joined`, `remakeLoop`, `remade`). `readAnswers` of `tasks_library.py` checks the proposals of the leader in the same way as the forms of the window.

</details>

## Contributing

This is the open-source repository of SwarmUP.

1. You are welcome to contribute to this repository. If you find a bug or have an idea to make the project better, send a pull request or open an issue.
2. Follow the contribution guidelines in [`CONTRIBUTING.md`](docs/CONTRIBUTING.md) and the code of conduct in [`CODE_OF_CONDUCT.md`](docs/CODE_OF_CONDUCT.md).
3. If you have a question or need help, speak to the maintainers of the project. They are glad to help you. The maintainers are volunteers, so be patient: an answer can take some time.

<div align="center">
<br>
<img src="src/user-interface/logo.svg" alt="" width="36"><br>
<sub>SwarmUP is licensed under the <a href="LICENSE">Apache License 2.0</a>.</sub>
</div>
