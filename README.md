# SwarmUP

> This is the open-source repository of SwarmUP.

1. You are welcome to contribute to this repository. If you find a bug or have an idea to make the project better, send a pull request or open an issue.
2. Follow the contribution guidelines in [`CONTRIBUTING.md`](docs/CONTRIBUTING.md) and the code of conduct in [`CODE_OF_CONDUCT.md`](docs/CODE_OF_CONDUCT.md).
3. If you have a question or need help, speak to the maintainers of the project. They are glad to help you. The maintainers are volunteers, so be patient: an answer can take some time.

## How does it work?

SwarmUP is an interactive user interface. You use it to design your own agents, and you connect them like building blocks (plug and play). You can adapt each agent to do a specific task, and you can connect it to different APIs and services.

The system is flexible, and you can extend it. Thus, you can make agents that adapt to different situations and needs. At the end of the design, the system gives you the code of the harness that you designed. With this code, it is easy to use the harness in other projects. Users who do not write code use their agents through the interface. Developers add the harness to their own projects with the code that they get.

You do not need experience with code. SwarmUP gives the important and frequently used harness tools as modules. These modules make it easier to make an agent if you do not want to design a custom harness. You can also make your own modules and share them with the community.

You can select well-known models, such as GPT, Claude and Gemini. You can run a free model on your computer or in the cloud. For a paid model, you must give your API key, and the provider bills you for its use.

Some tools, such as email, can also need credentials. You give your own credentials for these tools, and the system uses them to do the necessary tasks. The system is designed to be secure. It always keeps your credentials encrypted, on your computer, and only for a limited time.

## Repo Structure

- **`docs/`**: The documentation of the project.
- **`src/`**: The source code of the project: the user interface, the logic of the agents and other core utilities.
  - `src/backend/interface/`: the interface.
    - `user_interface.py` runs the interface in its own window, and `web_server.py` is the local server of the window.
    - `interface_views.py` prepares what the window shows.
    - `session_core.py` holds the `Session`, which connects the window to the swarm. Its parts are the steps of the form (`session_steps.py`), the models (`session_models.py`), the swarm that runs (`session_runs.py`) and the saved swarms (`session_saved.py`).
  - `src/backend/swarm-utils/`:
    - The tools that all parts use: `harness_utils.py` (the context files, the internet and the feeds), `internet_cache.py` (the data from the internet, kept for when there is no connection), `mission_costs.py`, `user_settings.py`, `gpu_check.py`, `messengers.py` (the messaging apps) and `saved_swarms.py`.
    - The loops of the agents: `base_loop.py` (the `Loop` that all loops are based on), `message_loops.py` (email, calendar and news), `writing_loops.py` (writing, papers, literature review and formatting) and `checking_loops.py` (math and code).
    - The swarm: `swarm_harness.py` holds the `Swarm`. Its members are in `swarm_team.py`, its reviews are in `swarm_review.py` and its run is in `swarm_run.py`.
    - The leader, which builds and manages the swarm: `leader_utils.py` (`designSwarm`), `leader_parser.py`, `leader_catalog.py`, `leader_checks.py` and `leader_manager.py`.
  - `src/backend/model-utils/`:
    - The lists of models (`models_library.py`) and the prompts (`agent_prompts.py`).
    - The clients of the models (`model_clients.py`), with their shared parts in `model_support.py`.
    - The coding agents: `coding_agents.py` for Claude Code and `codex_agent.py` for Codex.
  - `src/backend/task-utils/`: the tasks and their questions (`tasks_library.py`), and the lists that you select from (`sources_library.py`).
  - `src/user-interface/`: the pages of the interface (HTML, CSS, JavaScript and SVG icons). `index.html` loads the scripts in sequence, from `page_tools.js` to `app.js`. It loads the styles from `style.css` and `views.css`.
  - The names of the folders in `src/backend/` have hyphens, so these folders are not Python packages. Thus, the programs and the tests add each folder to the Python path.
- **`tests/`**: One test file for each part. `patching.py` has `everywhere`. For a test, this function replaces a function in all the modules that come from the same original file.
- **`agent-files/`**: The files for the functions of the agents, such as context files, memory files and other related resources. SwarmUP saves the state of each swarm that runs in `agent-files/swarm-runs/`.
- **`agent-rules/`**: The rules and configurations for the behavior and the decisions of the agents. Each agent must use these rules to make sure that it stays within the defined parameters and guidelines. Each agent has its own rules file.

## Available features

### Agents

Each agent does one task. First, it writes a draft. Then it checks the draft automatically (length, format, the rules of `agent-rules/`, links, missing words, proofs...) and makes it better. Then it shows the draft to you. You approve it, reject it or ask for changes. The agent acts only after your approval.

- **Email writer and sender:** It writes in the language that you select, in the tone of your earlier emails. It can read the latest reply from the recipient (IMAP). It sends (SMTP) the exact text that you approved. It has presets for Gmail, Outlook, Yahoo, iCloud and Zoho, and a login test that does not send anything.
- **Calendar planner:** It books an event that does not overlap with the other events. It exports `calendar_events.ics`, which you can import into Google Calendar, Outlook and Apple Calendar.
- **News briefer:** It reads the feeds of the news outlets that you select, from a list sorted by region and topic, or from any feed address. Then it writes a short briefing with the link of each story, immediately or at the time of day that you select. If you want, it also sends the approved briefing to your phone, on Telegram or WhatsApp, exactly as you approved it.
- **Writer:** It writes a text on a subject, with a maximum length, in the style of your earlier texts.
- **Literature reviewer:** It searches Google Scholar, arXiv, Crossref and OpenReview. You can limit the search to some publishers, from a list of the usual publishers or any publisher that it finds by name. It reads the abstracts and writes a survey. It checks each link of the survey against the papers that it found.
- **Document formatter:** It changes only the layout of a document, makes sure that no word is missing, and writes a new file. It never changes the original file.
- **Math checker:** It reviews a research text one theorem at a time, as a referee does, with quotes. Then a second check tries to prove that each gap or error that it reported is not real.
- **Coder:** It writes the code of a file. It runs a command to check the code, but only after you give permission.

### Folders

After you select the tasks, you can give each agent a folder on your computer to work in. You can also give it no folder. The agent saves what it makes in its folder: the formatted copy, the report, the briefing, the text, the survey, a copy of each email that it sent, and the calendar file. The files that the agent works on must be in its folder. The command that checks the code of the coder also starts in this folder.

### Messaging apps

The news briefer can send its briefing to Telegram or WhatsApp. The questions about these apps show only for this agent. The program checks the data that you give without sending a message. (On WhatsApp, the only way to check your own number is to send a message to it.) If a briefing is too long, the program sends it in parts.

- **Telegram:** Create a free bot with @BotFather in Telegram, and give its token. Then send a first message to your bot: the program finds your chat number from this message.
- **WhatsApp:** Create a free WhatsApp Business app at developers.facebook.com. Then give its access token, its phone number ID and your own number with its country code. WhatsApp lets such an app send free text only to a person who wrote to it in the last 24 hours. So, you must send a message to the number of the app before the time of the briefing. If you do not, WhatsApp refuses the message, and the program tells you why.

### Swarms

A swarm has one or more agents. The first agent is the leader.

- The agents work at the same time. An agent can wait for other agents and get their results. You select which agents wait, or the leader decides and you approve. If an agent fails, the swarm does not stop, and the leader gets this information.
- **Plan mode:** Each agent writes the plan of its task. The leader writes a summary of all the plans for your approval. **Execute mode:** Before the agents act, the leader writes a summary of what is done and what will happen next.
- You can approve, reject or correct the summary as a whole, or each agent alone. If you correct the summary, the leader sends the correction to the agents that it is about. If you correct one agent, only that agent changes.
- You can send a message to each agent, the leader included, while it works. The agent reads the message with its next prompt. If the agent did not complete its draft, it writes the draft again.
- **Add and remove agents while the swarm runs.** A new agent joins the agents at work, until the leader starts its final work. It can wait for agents that are already in the swarm, and it gets their results. When you remove an agent, it stops at its next step. SwarmUP restores the files where the agent did not complete its changes, and the model of the agent releases its memory (a GPU, for a local model). If the agent was finished, its result stays, and the agents that need it still get it. All the agents of the swarm get information about who joined or left. Thus, no agent waits for an agent that left.
- The tree of the swarm shows, live, which agent waits for which agent, what needs your approval, and which agents plan, execute or are finished.

### The leader can build the swarm

You can build the swarm one agent at a time, or you can let the leader build it. For the leader, you select its model and the folder of the mission, and you tell it the mission.

- The leader reads the mission, the names of the files in the folder, and the tasks that an agent can have, with their settings. It also reads the models that can run on your computer now: the local models that fit together in your GPUs, the API models (with or without a key), and Claude Code and Codex if they are ready. Then it proposes the full swarm: the agents, their tasks and settings, their models, and which agent waits for which. It gives a reason for each agent.
- You approve the proposal or reject it. You can also tell the leader in your own words what to change, and it writes the proposal again. After your approval, the agents are usual agents of the steps, so you can still change each of them before the run.
- While the swarm runs, SwarmUP asks the leader if the swarm needs a change. It asks each time an agent finishes or you write to the leader. The leader can propose to add an agent, to remove an agent (for example, to release a GPU), or to change the model of an agent that did not start. Each proposal has its reason and waits for your approval. If you reject a proposal, SwarmUP never shows it again.
- The leader never controls SwarmUP. SwarmUP tells the leader to write its proposals in blocks (`<swarmup_build>`, `<swarmup_add>`, `<swarmup_remove>`, `<swarmup_model>`), and it reads each answer of the leader. It finds the blocks even when their format is not exactly correct. It checks each proposal as it checks the forms that you complete: the task, the name, the model (which must fit in the GPUs with the other models), which agent waits for which, and each setting. If something is wrong, SwarmUP sends it back to the leader to write again, before you see it. SwarmUP never acts on a sentence that only suggests a change. It asks the leader to confirm the change in a block.
- SwarmUP never accepts passwords, tokens or API keys from the leader. It asks you for them. It also asks you for the settings that the leader cannot know, for example your own email address.
- At the end, the leader writes the final report of the mission from the results of the agents. After you approve the report, the leader saves it in the folder of the mission.

### Your work is never lost

A swarm works in its own thread, and SwarmUP saves its full state after each change.

- If the internet connection stops, the agents that need it pause; they do not fail. The leader tells you that the swarm has no connection. You can continue (SwarmUP tries the failed step again) or cancel. Before you cancel, the leader gives a summary of all the changes that are already done (emails sent, events booked, files written...). It also tells you which tasks will stop before they are complete. Then you select: stop at this point, or continue until the end. If the coder stops, SwarmUP restores the original of its file.
- If the program or the computer stops, the program finds the swarm when it starts again. It tells you that the swarm was interrupted. You can continue the swarm from where it stopped, or cancel it after the same summary. The program does not do again what is already done. It never sends an approved email or message two times. If the program stopped while it sent one, you decide if it sends it again.
- The program never saves passwords, API keys, tokens or accounts. So, it asks for them again when you continue a swarm.

### Models

Each agent has its own model.

- **Local models:** Dozens of open models (Qwen, Llama, Kimi, Mistral, DeepSeek, Gemma, Phi, gpt-oss, GLM, Granite, OLMo, Nemotron), which run on your GPUs. The list shows the VRAM that each model will probably need.
- **API models:** GPT (OpenAI), Claude (Anthropic), Gemini (Google) and DeepSeek, with their prices.
- **Coding agents:** Claude Code (Anthropic) and Codex (OpenAI), which run on your computer. They think for an agent as a model does. They can also read files, run commands and use the web, each time with your approval (refer to "Coding agents" below).
- Each task has recommended models, and you can also type the name of a different model.
- **First, SwarmUP shows only the models that fit:** The first list of models for an agent shows only the local models that fit in the VRAM that the other agents leave free. It also shows the API models whose price for 1 million tokens fits in the budget that the mission has left. The list of all the models always shows each model, with a mark on the models that do not fit. The leader gets the same lists.
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

The settings of the window are in `agent-files/settings.json`, and they apply to all your missions. One setting is the maximum number of agents that a leader can put in a swarm: 10 by default, from 1 to 100. You can always add more agents yourself.

### Coding agents

An agent can think with Claude Code or with Codex, instead of a model. The coding agent works in the folder of the agent. If the agent has no folder, it works in an empty folder of its own. **SwarmUP asks you before the coding agent does anything other than reading that folder**: a command, a change to a file, a web page, or a file outside its folder. The request shows in the conversation, with the exact command or file. You select **Allow once**, **Allow until the next run** or **Deny**, and you can tell it why. The questions of the coding agent also show there, with their options. The settings files in the folder cannot give a permission in advance, and a swarm that you stopped refuses all new requests.

- **Claude Code** uses your **Anthropic API key**, and you pay for each use, as with the API models. Anthropic does not let other programs use a Claude subscription ([Agent SDK documentation](https://code.claude.com/docs/en/agent-sdk/overview)). So, SwarmUP gives Claude Code its own configuration folder and clears the other credentials. If Claude Code says that it uses something other than the key, SwarmUP stops it. Install its library, which contains Claude Code: `pip install claude-agent-sdk` (Python 3.10 or later).
- **Codex** uses your **ChatGPT plan**. The first time, you sign in from SwarmUP: in your browser, or with a one-time code that SwarmUP shows. (For the code, enable the sign-in with a code in the security settings of ChatGPT.) Codex keeps the sign-in as it does when you use it alone, and SwarmUP never sees your password. Install Codex with `npm install -g @openai/codex@0.160` (it needs Node.js). SwarmUP communicates with Codex through its app-server, which OpenAI still calls experimental. Thus, SwarmUP accepts only the tested version (0.160.x). To try a different version, set `SWARMUP_ALLOW_UNTESTED_CODEX=1`. To give the path of the program, set `SWARMUP_CODEX`. In SwarmUP, the web search of Codex (which would not ask you), its sub-agents and your MCP servers are disabled. On Windows, SwarmUP asks you about each command of Codex, also the commands that only read.

### Safety

SwarmUP does nothing without your approval. It keeps API keys, passwords, tokens and accounts only in memory while the program runs. It never writes them to a file, not even in the saved state of a swarm.

### For developers

`Swarm.addListener` gives each change of a swarm as an event. `tasks_library.py` describes the questions of each task, and `models_library.py` holds the lists of models. The graphical interface (`src/backend/interface/user_interface.py`) uses these parts. Its `Session` (`session_core.py`) connects each loop to the window: `notifyUser`, `askUser` and `askSecret` become messages and questions in the conversation. The window asks the session for news with `/api/poll`.

- `Swarm.startInBackground` runs a swarm in its own thread. Use `isRunning` and `wait` to monitor it. `findUnfinishedSwarms` of `saved_swarms.py` finds the swarms that were interrupted, and `Swarm.restore` restores one of them. The interface builds its agents again, and `publicAnswers` and `restoreAnswers` of `tasks_library.py` keep the secrets out of the saved data. Then `resume` continues from where the swarm stopped.
- After the connection is lost (the events `connectionLost`, `resumed` and `stopped`), an interface answers with `continueWork`, `summarizeChanges` and `stopWork`, as the console does.
- `sources_library.py` lists the messaging apps (`MESSAGING_APPS`), and `messengers.py` has the code that sends to each app.
- A coding agent (`ClaudeCodeModel` in `coding_agents.py`, `CodexModel` in `codex_agent.py`) asks its questions through its loop, with `Loop.askPermission` and `Loop.askQuestions`. An interface replaces these functions, as it replaces `askUser` (the console and the window both do this). `Swarm.prepareRun` calls `newRun` on the coding agents. This function clears the permissions that were given until the next run.
- `Swarm.addAgent` and `Swarm.removeAgent` work while the swarm runs (the events `joined`, `removed` and `retired`). `Swarm.setModel` works for an agent that did not start (the event `model`).
- `remember` in `internet_cache.py` keeps the data that the program gets from the internet, with its age limit, and the last version for when there is no internet. `describeCached` tells the date of this data, and `keepFresh` updates the prices in the background.
- `MissionCosts` in `mission_costs.py` counts what a mission spends, with its budget. Each client records its calls with `recordCall` of `model_support.py`. `MissionCosts` has `report` for the screen, `left` for the decisions, and `warnings` at 80% and 100%. The loop calls `Loop.onUsage` after each call of a model, so an interface can show the cost as it increases. `loadSettings` and `saveSettings` of `user_settings.py` keep the settings of the user.
- The leader that builds the swarm:
  - `leader_parser.py` has `parseOutput`, which finds the blocks in a text, and `findSuggestions`, which finds the sentences that suggest a change.
  - `LeaderCatalog` gives the leader its information (in `leader_catalog.py`) and checks its proposals (in `leader_checks.py`).
  - `designSwarm` (in `leader_utils.py`) builds the swarm with the user.
  - `LeaderManager` (in `leader_manager.py`) is the leader while the swarm runs.
  - The leader asks the user through `Loop.askProposal`. The program that runs the swarm makes the agents for the leader (`makeLoop`, `joined`, `remakeLoop`, `remade`). `readAnswers` of `tasks_library.py` checks the proposals of the leader in the same way as the forms of the window.

## Use the interface

```bash
python src/backend/interface/user_interface.py
```

`python src/backend/interface/user_interface.py` opens SwarmUP in its own window, on Windows, macOS and Linux.

The interface guides you through six steps. Each step has a guide on the side.

1. **The mission**, and who builds the swarm: you, or the leader. The leader shows its proposal in a window, where you approve it, reject it or ask for changes.
2. **The agents**: one card for each agent, with the questions of its task. You can check your email login or your messaging app without sending anything. If you remove an agent, you can use Undo.
3. **The folders**, with the folder dialog of your system.
4. **The models**: local models with a gauge of the VRAM of your GPUs, API models with their prices and your key, or a coding agent (Claude Code with your key, or Codex with the sign-in to your ChatGPT plan).
5. **Who waits for whom**: you select, or the leader proposes and you approve.
6. **The launch**: plan first, or execute immediately.

Then you follow the swarm live on a map. A golden light shows the agents that wait for you. The leader asks its questions in a conversation on the side, with buttons for the usual answers. You can:

- approve, reject or correct each agent alone,
- send a message to an agent while it works,
- start an agent that waits for its time,
- add an agent or remove an agent,
- stop the swarm, after the leader tells you what is already done.

If the leader built the swarm, its proposals show in the conversation, with their reason and the buttons to approve or reject them. The home page shows each swarm that was interrupted. You can continue it (you give the passwords and keys again) or cancel it.

- For the window, install pywebview: `pip install pywebview`. On Linux, it also needs GTK or Qt: `pip install pywebview[gtk]` or `pip install pywebview[qt]`. It uses the web engine of the system: WebView2 on Windows (already installed on Windows 10 and 11) and WebKit on macOS. Without pywebview, SwarmUP opens in an application window of Edge, Chrome, Chromium or Brave, if one of them is installed. If not, it opens in a tab of your web browser. To select the window yourself, use `--window browser` (or `app`, `webview`, `none`).
- The interface accepts connections only from your own computer (127.0.0.1), and only the window that it opened can use it. SwarmUP never sends passwords, keys or tokens back to the window, and never writes them to a file.
- When you close the window, the program stops. If a swarm runs, SwarmUP saves it first, so you can continue it at the next start.

## Try it in the command line

```bash
python tests/full_command_line_user_test.py
```

`python tests/full_command_line_user_test.py` lets you build and run a swarm. You answer questions that guide you:

- who builds the swarm: you, or the leader, whose proposal you approve,
- how many agents the swarm has,
- the task of each agent (email, writing, coding, math checking, literature review, news briefing...) and what it needs to work,
- the folder of each agent,
- the model of each agent: a local model on your GPUs (with the VRAM that it needs and a check of your GPUs), a paid model through an API (with the prices), or a coding agent (Claude Code or Codex, which ask you before they act).

Then the program shows the tree of the swarm live, while the agents plan or execute. At any time, you can approve or correct each agent, or send it a message. You can also add an agent (`add`) or remove one (`remove <agent> <why>`). Type `help` to see all the commands.

- SwarmUP does nothing before you approve it. It sends emails, books events and writes files only after you approve the exact result.
- If the connection stops, the swarm pauses and asks you to type `continue` or `cancel`. If the program or the computer stops, start the program again. It lets you continue the swarm that was interrupted, or cancel it after a summary of what it did.
- API models need their library (`pip install anthropic` for Claude, `pip install openai` for GPT, Gemini and DeepSeek) and a key. The program reads the key from `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY` or `DEEPSEEK_API_KEY`, or you type it when the program asks. The program keeps keys and passwords only in memory.
- Local models need `pip install torch transformers accelerate` (and `bitsandbytes` for compressed models). They also need an NVIDIA GPU (Linux and Windows) or an AMD GPU (Linux). The program downloads them to the Hugging Face cache, which is the folder in the `HF_HOME` environment variable.
- `python tests/live_checks.py gpus` shows what the GPU check finds on your computer. `python -m unittest discover -s tests` runs the tests.
- The tests of the coding agents (`tests/test_coding_agents.py`) run the real Claude Code and Codex with a fake model on your computer. So, they need no key and no account. If `claude-agent-sdk` or Codex is not installed, these tests are skipped.
