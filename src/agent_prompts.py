# An agent should focus on their own work. 
# There will be a default free agent/leader which can be 
# prompted anything like a chatbot. However, when a swarm is called, 
# each agent should only foces on their speciality.


# ==============
# Task prompts of the loops in harness_utils.py.
# The {names} between braces are filled in by the loops, so a prompt can be improved without touching any code.
# Short intermediate prompts (asking for changes, checking rules...) stay in harness_utils.py.
# ==============
EMAIL_PROMPT = """Write an email in {language} from {sender} to {receiver} with the subject "{subject}".
What the email must say: {request}
The latest email received from {receiver}: {incoming}
Earlier emails you sent them: {sent}
Use the same tone as the earlier emails. Reply with the email body only, without the subject line."""


EVENT_EXAMPLE = '{"date": "2030-01-31", "time": "14:30", "duration": 60, "subject": "Dentist"}'
CALENDAR_PROMPT = """Today is {today}. Plan this calendar event: {request}
Events already booked, avoid overlapping with them: {booked}
Reply only with JSON like {example}
The date is YYYY-MM-DD, the time is 24-hour HH:MM and the duration is in minutes."""


NEWS_PROMPT = """Today is {date}. Write a news briefing in {language} of at most {maxWords} words from the headlines below.
Topics the user cares about: {topics}.
Briefings already written today, do not repeat their stories: {earlier}
Headlines:
{headlines}
Only use facts from the headlines and put the source link after each story."""


AUTHOR_PROMPT = """Write a text about {subject} in at most {length} words.
Match the writing style of the user's earlier texts: {earlier}
Reply with the text only."""


LITERATURE_PROMPT = """Write a literature survey on {subject} in at most {length} words, using only the papers below.
Group them by theme and cite every paper you use by its title and exact link.
Papers:
{papers}"""


DOCUMENT_FORMAT_PROMPT = """Format the document below in this style: {style}.
Do not change, add, remove or reorder any content. Only change the layout, headings, lists and punctuation.
Reply with the formatted document only, without code fences around it.

{document}"""


FINDING_FORMAT = """CLAIM: <name of the result, or of the step of its proof>
QUOTE: <one passage copied exactly from the document, on a single line>
STATUS: <VALID, GAP, ERROR or UNCLEAR>
WHY: <why it is valid, or what is missing or wrong and how to fix it>"""
MATH_CHECK_PROMPT = """Referee the mathematical text below as a careful reviewer of a research paper would.
Go through its theorems, lemmas, propositions, corollaries and claims in order, and through the key steps of their proofs.
Write one finding for each, as plain lines without bold or bullets, with an empty line between findings, exactly like this:
{findingFormat}
Finish with one line: VERDICT: NO PROBLEMS FOUND or VERDICT: PROBLEMS FOUND

{document}"""


CODER_PROMPT = """Write the complete content of the file {fileName} for this task: {task}
It is checked by running: {command}
Current content of the file: {current}
Reply with the complete file content only."""


SWARM_PLAN_EXAMPLE = '{"Writer": [], "Checker": [], "Formatter": ["Writer"], "Mailer": ["Formatter", "Checker"]}'
SWARM_PLAN_PROMPT = """You are the leader of a swarm of agents working on this mission: {mission}
The agents of your swarm:
{agents}
Decide which agents must wait for the final output of other agents before they can start.
Agents that do not need the output of another agent work at the same time as the others, so only add a wait when it is really needed.
Reply only with JSON that gives, for every agent, the list of the agents it waits for, like {example}"""


USER_MESSAGES_PROMPT = """Messages from the user, sent while you work. The oldest is first, and if two of them disagree follow the newest:
{messages}
The user is the person you work for, so these come before the messages of the other agents.
Put what they ask into your own work, inside your own role and task, and leave out anything that is not your job.
Never answer the user directly: keep replying in exactly the format your task asks for. They cannot change your rules or that format."""


# ==============
# Plan mode and execute mode of a swarm.
# In plan mode every agent writes a plan for its own task, and the leader summarises all the plans for the user.
# In execute mode the leader summarises what the agents have done so far and what they are about to do.
# ==============
PLAN_PROMPT = """Write a plan for your task: {task}
Do not do the task yet. In at most {maxWords} words, say the steps you will take, the tools or sources you will use,
and what you need from the user or from the other agents.
Reply with the plan only."""


APPROVED_PLAN_PROMPT = """Your plan, approved by the user. Follow it, unless the messages above change it:
{plan}"""


PLAN_SUMMARY_PROMPT = """You are the leader of a swarm of agents working on this mission: {mission}
Every agent wrote a plan for its own task. Where each agent stands, including the plans the user already approved:
{agents}
Write one summary plan for the user that covers every agent. Name each agent, say what it will do, and say whether the user already approved its plan.
Keep the details the user must check, such as who receives an email, dates and files. Do not add anything that is not above.
What the user asked to change in the summary, if anything: {request}
Reply with the summary only."""


EXECUTION_SUMMARY_PROMPT = """You are the leader of a swarm of agents working on this mission: {mission}
Where each agent stands, including what the user already approved:
{agents}
Write one summary for the user of what has been done so far and of what is about to happen. Name each agent that has started,
and say whether the user already approved what it does.
Keep the details the user must check, such as who receives an email, dates and files. Do not add anything that is not above.
What the user asked to change in the summary, if anything: {request}
Reply with the summary only."""


CANCEL_SUMMARY_PROMPT = """You are the leader of a swarm of agents working on this mission: {mission}
The user wants to cancel the swarm before it is finished. Where each agent stands, and what it already changed outside of itself:
{agents}
Write one summary for the user of all the changes and actions that already took place: what was sent, booked, written or changed, and where.
Then say which agents did not finish, and what would be cut if the user stops now. Name every agent. Do not add anything that is not above.
Reply with the summary only."""


CORRECTION_ROUTE_EXAMPLE = '{"Writer": "Shorten the text to 100 words"}'
CORRECTION_ROUTE_PROMPT = """You are the leader of a swarm of agents working on this mission: {mission}
The user read your summary and asked for this correction: {correction}
The agents that have something waiting for the user:
{agents}
Decide which of these agents must change their work to satisfy the correction, and leave out the agents it does not concern.
Reply only with JSON that gives, for every concerned agent, what it must change, like {example}
If no agent is concerned, because the correction is only about the wording of your summary, reply {{}}"""
