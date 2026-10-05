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
- **`src/`**: Contains the source code for the project including the user interface, agent logic, and other core utilities. `user_interface.py` runs the interface, and its pages (HTML, CSS, JavaScript and SVG icons) are in `src/user-interface/`.
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
- The tree of the swarm shows live who waits for whom, what needs your approval, and what is planning, executing or done.

### Your work is never lost

A swarm works in its own thread, and its whole state is saved at every change.

- If the internet is lost, the agents that need it are paused instead of failed, and the leader tells you that the swarm lost its connection. You continue (the step that failed is tried again) or you cancel. To cancel, the leader first summarises everything that was already changed (emails sent, events booked, files written...) and what would be cut in the middle of its task, and you confirm to stop there or go on until the end. If the coder is stopped, the original of its file is put back.
- If the program or the computer stops, the next start of the program finds the swarm, tells you it was interrupted, and offers to continue it where it stopped or to cancel it, with the same summary. What is done is not done again. An email or a message you approved is never sent twice, and if the program stopped while sending one, you decide if it is sent again.
- Passwords, API keys, tokens and accounts are never saved, so the program asks for them again when a swarm is continued.

### Models

Every agent has its own model.

- **Local models:** dozens of open models (Qwen, Llama, Kimi, Mistral, DeepSeek, Gemma, Phi, gpt-oss, GLM, Granite, OLMo, Nemotron) with the VRAM each one is expected to need, run on your GPUs.
- **API models:** GPT (OpenAI), Claude (Anthropic), Gemini (Google) and DeepSeek, with their prices.
- Recommended models for each task, and the name of any other model can be typed.
- **GPU check:** the total and the free VRAM of your GPUs (NVIDIA on Linux and Windows, AMD on Linux), and the VRAM the swarm is expected to need, which grows with each agent. A local model that does not fit cannot be chosen. One that fits but is not free now gives a warning, and the swarm does not start until the memory is free. The Windows part is written but has not been tried on a real Windows computer yet.

### Safety

Nothing is done without your approval. API keys, passwords, tokens and accounts are only kept in memory while the program runs, and never written to a file, not even in the saved state of a swarm.

### For developers

`Swarm.addListener` gives every change of a swarm as an event, `tasks_library.py` describes the questions of every task, and `models_library.py` holds the model lists. The graphical interface (`src/user_interface.py`) is built on them: its `Session` connects every loop to the window (`notifyUser`, `askUser` and `askSecret` become messages and questions of the conversation), and the window asks the session for news with `/api/poll`.

- `Swarm.startInBackground` runs a swarm in its own thread, followed with `isRunning` and `wait`. `findUnfinishedSwarms` finds the swarms that were interrupted, `Swarm.restore` brings one back (the interface builds its agents again, and `publicAnswers` and `restoreAnswers` of `tasks_library.py` keep the secrets out of what is saved), and `resume` goes on where it stopped.
- After a lost connection (the events `connectionLost`, `resumed` and `stopped`), an interface answers with `continueWork`, `summarizeChanges` and `stopWork`, as the console does.
- `sources_library.py` lists the messaging apps (`MESSAGING_APPS`), and `harness_utils.py` has the code that sends to each one.

## Use the interface

```bash
python src/user_interface.py
```

`python src/user_interface.py` opens SwarmUP in a window of its own, on Windows, macOS and Linux.

It guides you through six steps, each with a guide on the side: **the mission**, **the agents** (one card per agent, with the questions of its task, checks of your email login or messaging app that send nothing, and Undo if you remove one), **the folders** (with the folder dialog of your system), **the models** (local ones with a gauge of the VRAM of your GPUs, or API ones with their prices and your key), **who waits for whom** (you choose, or the leader proposes and you approve), and **the launch** (plan first, or execute right away).

The swarm is then followed live on a map: a golden light shows the agents that wait for you, and the leader asks its questions in a conversation on the side, with buttons for the usual answers. You can approve, reject or correct any agent on its own, message it while it works, start an agent that waits for its time, or stop the swarm after the leader tells you what was already done. A swarm that was interrupted is offered on the home page, to continue it (you give the passwords and keys again) or to cancel it.

- For the window, install pywebview: `pip install pywebview` (on Linux it also needs GTK or Qt: `pip install pywebview[gtk]` or `pip install pywebview[qt]`). It uses the web engine of the system: WebView2 on Windows (already installed on Windows 10 and 11), WebKit on macOS. Without pywebview, SwarmUP opens in an application window of Edge, Chrome, Chromium or Brave if one is installed, and otherwise in a tab of your web browser. `--window browser` (or `app`, `webview`, `none`) chooses it yourself.
- The interface only listens to your own computer (127.0.0.1), and only the window it opened can use it. Passwords, keys and tokens are never sent back to the window, nor written to a file.
- Closing the window ends the program. A swarm that runs is saved first, so it can be continued at the next start.

## Try it in the command line

```bash
python tests/full_command_line_user_test.py
```

`python tests/full_command_line_user_test.py` lets you build and run a swarm by answering guiding questions: how many agents, the task of each one (email, writing, coding, math checking, literature review, news briefing...) with what it needs to work, the folder of each one, and the model of each one, local on your GPUs (with the VRAM it needs and a check of your GPUs) or paid through an API (with the prices). The tree of the swarm is then shown live while the agents plan or execute, and you can approve, correct or message any agent at any time (type `help`).

- Nothing is done before you approve it: emails are only sent, events booked and files written after you approve the exact result.
- If the connection is lost, the swarm pauses and asks you to type `continue` or `cancel`. If the program or the computer stops, start the program again: it offers to continue the swarm that was interrupted, or to cancel it after a summary of what it did.
- API models need their library (`pip install anthropic` for Claude, `pip install openai` for GPT, Gemini and DeepSeek) and a key, read from `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY` or `DEEPSEEK_API_KEY`, or typed when the program asks. Keys and passwords are only kept in memory.
- Local models need `pip install torch transformers accelerate` (and `bitsandbytes` for compressed models) and an NVIDIA GPU (Linux and Windows) or an AMD GPU (Linux). They are downloaded to the Hugging Face cache, which is the folder of the `HF_HOME` environment variable.
- `python tests/live_checks.py gpus` shows what the GPU check finds on your computer, and `python -m unittest discover -s tests` runs the tests.
