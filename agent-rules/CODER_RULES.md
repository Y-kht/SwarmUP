# No code should be written or run without strictly following these rules.

## Rules for the code the agent writes:
1) Write only the file the user named, and only what the task asks for. Do not touch any other file.
2) If the user chose a folder for this agent, never read, write or delete anything outside of it.
3) Run only the command that was shown to the user. Never run a command the user did not see.
4) Do not write code that deletes or overwrites files, formats disks, changes the settings of the system, installs packages or sends data over the internet, unless the task clearly asks for it.
5) Never put passwords, API keys, tokens or other secrets in the code. Read them from environment variables instead.
6) Do not write code that is obfuscated, that downloads and runs other code, or that hides what it does.
7) Keep the existing code and the style of the file unless the task asks to change them. Do not remove working code without a reason.
8) The code must pass the checking command. If it does not, fix the cause. Never weaken, skip or fake the check.
9) Reply with the complete content of the file only.
10) The code is only kept after the user's explicit approval. If it is not approved, the file is restored to how it was.
