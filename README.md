# SwarmUP

> This is the open source repo of SwarmUP.

1. Feel free to contribute to this repo. You can submit a pull request or raise an issue if you find any bugs or have suggestions for improvements.
2. Please make sure to follow the contribution guidelines outlined in the [`CONTRIBUTING.md`](docs/CONTRIBUTING.md) file as well as the code of conduct in the [`CODE_OF_CONDUCT.md`](docs/CODE_OF_CONDUCT.md) file.
3. If you have any questions or need assistance, please reach out to the maintainers of the project. They are happy to help and provide guidance. As the maintainers are volunteers, please be patient and allow some time for them to respond.

## How does it work?

SwarmUP is designed to be an interactive user interface that allows users to design their own agents in a plug-and-play manner. The agents can be customized to perform specific tasks and can be integrated with various APIs and services.

The system is built to be flexible and extensible, allowing users to create agents that can adapt to different scenarios and requirements. To facilitate the integration of the designed harness in other projects, the system provides written code for the user-designed harness. This means that normal users can use their agents through the UI, while developers can integrate the harness into their own projects by using the provided code at the end of the design process.

No code experience is required for users. Some key and common harness tools are provided as modules to ease the process of agent creation for users with no interest in creating custom harnesses. Users can also create their own modules and share them with the community. Famous models like GPT, Claude, and Gemini are available for the users to pick. If a model is free, it can be run locally or on the cloud. If a model is paid, the API key must be provided by the user and will be billed.

The system can also require credentials for certain tools such as email. Users can provide their own credentials for these tools, and the system will use them to perform the required tasks. The system is designed to be secure and will always store the user credentials locally and temporarily with encryption.

## Repo Structure

- **`docs/`**: Contains documentation related to the project.
- **`src/`**: Contains the source code for the project including the user interface, agent logic, and other core utilities.
  - `src/backend/interface/`: `user_interface.py`, which runs the interface.
  - `src/backend/swarm-utils/`: `harness_utils.py` (the agents, their loops and the swarm) and `leader_utils.py` (the leader that builds and manages the swarm).
  - `src/backend/model-utils/`: the model lists (`models_library.py`), the clients of the models (`model_clients.py`) and the prompts (`agent_prompts.py`).
  - `src/backend/task-utils/`: the tasks and their questions (`tasks_library.py`) and the lists to pick from (`sources_library.py`).
  - `src/user-interface/`: the pages of the interface (HTML, CSS, JavaScript and SVG icons).
  - The folders of `src/backend/` have hyphens in their names, so they are not Python packages: the programs and the tests put each of them on the path.
- **`agent-files/`**: Contains files related to the agent functionality. This includes context files, memory files, and other related resources. The state of a swarm that is running is saved in `agent-files/swarm-runs/`.
- **`agent-rules/`**: Contains rules and configurations for the agent's behavior and decision-making processes. The agent must refer to these rules to ensure it operates within the defined parameters and guidelines. Every agent has its own rules file.

## Available features

### Agents

Every agent does one task. It writes a draft, checks it automatically (length, format, rules of `agent-rules/`, links, lost words, proofs...), improves it, and shows it to you. You approve, reject or ask for changes, and only then does it act.

- **Email writer and sender:** writes in the language you choose and in the tone of your earlier emails, can read the latest reply of the receiver (IMAP), and sends (SMTP) the exact text you approved. It has presets for Gmail, Outlook, Yahoo, iCloud and Zoho, and a login test that sends nothing.
- **Calendar planner:** books an event without overlapping the others and exports `calendar_events.ics`, which Google Calendar, Outlook and Apple Calendar import.
- **News briefer:** reads the feeds of the outlets you pick (a list grouped by region and topic, or any feed address) and writes a short briefing with the link of every story, right away or at the time of the day you choose. If you want, the approved briefing is also sent to your phone, on Telegram or on WhatsApp, exactly as you approved it.
- **Writer:** writes a text on a subject with a maximum length, in the style of your earlier texts.
- **Literature reviewer:** searches Google Scholar, arXiv, Crossref and OpenReview, can be restricted to publishers (a list of the common ones, or any publisher found by its name), reads the abstracts, and writes a survey whose links are checked against the papers it found.
- **Document formatter:** changes only the layout of a document, checks that no word was lost, and writes a new file. The original is never modified.
- **Math checker:** referees a research text theorem by theorem with quotes, and a second check tries to refute every gap or error it reports.
- **Coder:** writes the code of a file and checks it by running a command, after your permission.

### Folders

After choosing the tasks, you can give each agent a folder of your computer to work inside, or none. What the agent makes is saved inside it (the formatted copy, the report, the briefing, the text, the survey, a copy of every email sent, the calendar file). The files it works on must be inside the folder, and the command that checks the code of the coder starts in it.

### Messaging apps

The news briefer can send its briefing to Telegram or to WhatsApp, and the questions only appear for that agent. The program checks what you give it without sending anything (on WhatsApp your own number can only be checked by sending to it), and a briefing that is too long is sent in parts.

- **Telegram:** you create a bot for free with @BotFather in Telegram and give its token. The program finds your chat number from the first message you send to your bot.
- **WhatsApp:** you create a WhatsApp Business app for free at developers.facebook.com and give its access token, its phone number ID and your own number with its country code. WhatsApp only lets such an app write free text to someone who wrote to it in the last 24 hours, so you must send a message to the number of the app before the briefing is due. If you did not, WhatsApp refuses the message and the program tells you why.

### Swarms

A swarm has one or more agents, and the first one is the leader.

- The agents work at the same time. An agent can wait for others (you choose, or the leader decides and you approve) and receives their results. An agent that fails never stops the swarm, and the leader is told.
- **Plan mode:** every agent writes the plan of its task, and the leader summarises all the plans for your approval. **Execute mode:** the leader summarises what is done and what is about to happen, before the agents act.
- You can approve, reject or correct the summary as a whole, or any agent on its own. A correction of the summary is passed by the leader to the agents it concerns, and a correction of one agent only changes that agent.
- You can message any agent, the leader included, while it works. It reads the message with its next prompt, and a draft that was being written is written again.
- **Add and remove agents while the swarm runs.** A new agent joins the agents at work (until the leader starts its final work): it can wait for agents already there and receives their results. An agent that is removed stops at its next step, what it left half done in your files is put back, and its model frees its memory (a GPU, for a local model). One that had finished keeps its result, and the agents that need it still get it. Every agent of the swarm is told who joined or left, so none of them waits for an agent that left.
- The tree of the swarm shows live who waits for whom, what needs your approval, and what is planning, executing or done.

### The leader can build the swarm

Instead of building the swarm agent by agent, you can let the leader build it. You choose its model and the folder of the mission, and you tell it the mission.

- The leader reads the mission, the names of the files of the folder, the tasks an agent can have with their settings, and the models that can run on your computer now (the local models that fit in your GPUs together, the API models with a key or not, Claude Code and Codex if they are ready). It proposes the whole swarm: the agents, their tasks and settings, their models and who waits for whom, with a reason for each agent.
- You approve the proposal, reject it, or tell the leader in your own words what to change, and it writes it again. Once approved, the agents are ordinary agents of the steps, so you can still change any of them before the run.
- While the swarm runs, the leader is asked if the swarm needs a change each time an agent finishes or you write to it. It can propose to add an agent, to remove one (to free a GPU, for example), or to change the model of an agent that did not start. Every proposal comes with its reason and waits for your approval. One you rejected is never shown again.
- The leader never controls SwarmUP. It is told to write its proposals in blocks (`<swarmup_build>`, `<swarmup_add>`, `<swarmup_remove>`, `<swarmup_model>`), and SwarmUP reads every answer of the leader. It finds the blocks even when they are not written exactly as asked, and checks each proposal like the forms of the user: the task, the name, the model (it must fit in the GPUs with the others), who waits for whom and every setting. What is wrong goes back to the leader to be written again, before you see anything. A sentence that only suggests a change is never acted upon: the leader is asked to confirm it in a block.
- Passwords, tokens and API keys are never taken from the leader. SwarmUP asks you for them, and for the settings the leader could not know (your own email address, for example).
- At the end, the leader writes the final report of the mission from the results of the agents, and saves it in the folder of the mission after you approve it.

### Your work is never lost

A swarm works in its own thread, and its whole state is saved at every change.

- If the internet is lost, the agents that need it are paused instead of failed, and the leader tells you that the swarm lost its connection. You continue (the step that failed is tried again) or you cancel. To cancel, the leader first summarises everything that was already changed (emails sent, events booked, files written...) and what would be cut in the middle of its task, and you confirm to stop there or go on until the end. If the coder is stopped, the original of its file is put back.
- If the program or the computer stops, the next start of the program finds the swarm, tells you it was interrupted, and offers to continue it where it stopped or to cancel it, with the same summary. What is done is not done again. An email or a message you approved is never sent twice, and if the program stopped while sending one, you decide if it is sent again.
- Passwords, API keys, tokens and accounts are never saved, so the program asks for them again when a swarm is continued.

### Models

Every agent has its own model.

- **Local models:** dozens of open models (Qwen, Llama, Kimi, Mistral, DeepSeek, Gemma, Phi, gpt-oss, GLM, Granite, OLMo, Nemotron) with the VRAM each one is expected to need, run on your GPUs.
- **API models:** GPT (OpenAI), Claude (Anthropic), Gemini (Google) and DeepSeek, with their prices.
- **Coding agents:** Claude Code (Anthropic) and Codex (OpenAI), running on your computer. They think for an agent like a model, and they can also read files, run commands and use the web, each time with your approval (see Coding agents below).
- Recommended models for each task, and the name of any other model can be typed.
- **GPU check:** the total and the free VRAM of your GPUs (NVIDIA on Linux and Windows, AMD on Linux), and the VRAM the swarm is expected to need, which grows with each agent. A local model that does not fit cannot be chosen. One that fits but is not free now gives a warning, and the swarm does not start until the memory is free. The Windows part is written but has not been tried on a real Windows computer yet.

### Coding agents

An agent can think with Claude Code or with Codex instead of a model. It works in the folder of the agent (or in an empty folder of its own if the agent has none), and **SwarmUP asks you before it does anything other than reading that folder**: a command, a change of a file, a web page, a file outside its folder. The request appears in the conversation, with the exact command or file, and you choose **Allow once**, **Allow until the next run**, or **Deny** (you can tell it why). Its own questions appear there too, with their options. Settings files in the folder cannot pre-allow anything, and a stopped swarm refuses every new request.

- **Claude Code** runs with your **Anthropic API key**, billed per use like the API models. Anthropic does not allow other programs to use a Claude subscription ([Agent SDK documentation](https://code.claude.com/docs/en/agent-sdk/overview)), so SwarmUP gives Claude Code a configuration folder of its own, blanks the other credentials, and stops it if it says it uses anything other than the key. Install its library, which contains Claude Code itself: `pip install claude-agent-sdk` (Python 3.10 or later).
- **Codex** runs with your **ChatGPT plan**. The first time, you sign in from SwarmUP: in your browser, or with a one-time code shown in SwarmUP (for the code, turn on the sign-in with a code in the security settings of ChatGPT). Codex keeps the sign-in like it does for itself; SwarmUP never sees your password. Install Codex with `npm install -g @openai/codex@0.160` (it needs Node.js). SwarmUP talks to it through its app-server, which OpenAI still calls experimental, so only the tested version (0.160.x) is accepted: set `SWARMUP_ALLOW_UNTESTED_CODEX=1` to try another one, and `SWARMUP_CODEX` to give the path of the program. The web search of Codex (which would not ask), its sub-agents and your MCP servers are switched off inside SwarmUP. On Windows every command of Codex is asked, reads too.

### Safety

Nothing is done without your approval. API keys, passwords, tokens and accounts are only kept in memory while the program runs, and never written to a file, not even in the saved state of a swarm.

### For developers

`Swarm.addListener` gives every change of a swarm as an event, `tasks_library.py` describes the questions of every task, and `models_library.py` holds the model lists. The graphical interface (`src/backend/interface/user_interface.py`) is built on them: its `Session` connects every loop to the window (`notifyUser`, `askUser` and `askSecret` become messages and questions of the conversation), and the window asks the session for news with `/api/poll`.

- `Swarm.startInBackground` runs a swarm in its own thread, followed with `isRunning` and `wait`. `findUnfinishedSwarms` finds the swarms that were interrupted, `Swarm.restore` brings one back (the interface builds its agents again, and `publicAnswers` and `restoreAnswers` of `tasks_library.py` keep the secrets out of what is saved), and `resume` goes on where it stopped.
- After a lost connection (the events `connectionLost`, `resumed` and `stopped`), an interface answers with `continueWork`, `summarizeChanges` and `stopWork`, as the console does.
- `sources_library.py` lists the messaging apps (`MESSAGING_APPS`), and `harness_utils.py` has the code that sends to each one.
- A coding agent (`ClaudeCodeModel`, `CodexModel` in `model_clients.py`) asks through its loop: `Loop.askPermission` and `Loop.askQuestions`, which an interface replaces like `askUser` (the console and the window both do). `Swarm.prepareRun` calls `newRun` on them, which forgets what was allowed until the next run.
- `Swarm.addAgent` and `Swarm.removeAgent` work while the swarm runs (the events `joined`, `removed` and `retired`), and `Swarm.setModel` for an agent that did not start (the event `model`).
- `leader_utils.py` holds the leader that builds the swarm: `parseOutput` (the blocks in a text), `findSuggestions` (the sentences that suggest a change), `LeaderCatalog` (what the leader is told, and the checks of its proposals), `designSwarm` (the building of the swarm with the user) and `LeaderManager` (the leader while the swarm runs). The leader asks the user through `Loop.askProposal`, and the program that runs the swarm makes the agents for it (`makeLoop`, `joined`, `remakeLoop`, `remade`). `readAnswers` of `tasks_library.py` checks the proposals of the leader and the forms of the window the same way.

## Use the interface

```bash
python src/backend/interface/user_interface.py
```

`python src/backend/interface/user_interface.py` opens SwarmUP in a window of its own, on Windows, macOS and Linux.

It guides you through six steps, each with a guide on the side: **the mission** (and who builds the swarm: you, or the leader, which proposes it in a window where you approve it, reject it, or ask for changes), **the agents** (one card per agent, with the questions of its task, checks of your email login or messaging app that send nothing, and Undo if you remove one), **the folders** (with the folder dialog of your system), **the models** (local ones with a gauge of the VRAM of your GPUs, API ones with their prices and your key, or a coding agent: Claude Code with your key, or Codex with the sign-in to your ChatGPT plan), **who waits for whom** (you choose, or the leader proposes and you approve), and **the launch** (plan first, or execute right away).

The swarm is then followed live on a map: a golden light shows the agents that wait for you, and the leader asks its questions in a conversation on the side, with buttons for the usual answers. You can approve, reject or correct any agent on its own, message it while it works, start an agent that waits for its time, add an agent or remove one, or stop the swarm after the leader tells you what was already done. The proposals of a leader that built the swarm appear in the conversation, with their reason and the buttons to approve or reject them. A swarm that was interrupted is offered on the home page, to continue it (you give the passwords and keys again) or to cancel it.

- For the window, install pywebview: `pip install pywebview` (on Linux it also needs GTK or Qt: `pip install pywebview[gtk]` or `pip install pywebview[qt]`). It uses the web engine of the system: WebView2 on Windows (already installed on Windows 10 and 11), WebKit on macOS. Without pywebview, SwarmUP opens in an application window of Edge, Chrome, Chromium or Brave if one is installed, and otherwise in a tab of your web browser. `--window browser` (or `app`, `webview`, `none`) chooses it yourself.
- The interface only listens to your own computer (127.0.0.1), and only the window it opened can use it. Passwords, keys and tokens are never sent back to the window, nor written to a file.
- Closing the window ends the program. A swarm that runs is saved first, so it can be continued at the next start.

## Try it in the command line

```bash
python tests/full_command_line_user_test.py
```

`python tests/full_command_line_user_test.py` lets you build and run a swarm by answering guiding questions: who builds the swarm (you, or the leader, whose proposal you approve), how many agents, the task of each one (email, writing, coding, math checking, literature review, news briefing...) with what it needs to work, the folder of each one, and the model of each one, local on your GPUs (with the VRAM it needs and a check of your GPUs), paid through an API (with the prices), or a coding agent (Claude Code or Codex, which ask you before they act). The tree of the swarm is then shown live while the agents plan or execute, and you can approve, correct or message any agent at any time, add an agent (`add`) or remove one (`remove <agent> <why>`). Type `help` to see every command.

- Nothing is done before you approve it: emails are only sent, events booked and files written after you approve the exact result.
- If the connection is lost, the swarm pauses and asks you to type `continue` or `cancel`. If the program or the computer stops, start the program again: it offers to continue the swarm that was interrupted, or to cancel it after a summary of what it did.
- API models need their library (`pip install anthropic` for Claude, `pip install openai` for GPT, Gemini and DeepSeek) and a key, read from `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY` or `DEEPSEEK_API_KEY`, or typed when the program asks. Keys and passwords are only kept in memory.
- Local models need `pip install torch transformers accelerate` (and `bitsandbytes` for compressed models) and an NVIDIA GPU (Linux and Windows) or an AMD GPU (Linux). They are downloaded to the Hugging Face cache, which is the folder of the `HF_HOME` environment variable.
- `python tests/live_checks.py gpus` shows what the GPU check finds on your computer, and `python -m unittest discover -s tests` runs the tests.
- The tests of the coding agents (`tests/test_coding_agents.py`) run the real Claude Code and Codex against a fake model on your computer, so they need no key and no account. They are skipped when `claude-agent-sdk` or Codex is not installed.
