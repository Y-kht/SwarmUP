# The loops that write to someone or plan for the user: the email writer, the calendar planner and the news briefer.
import imaplib
import json
import os
import re
import smtplib
from datetime import datetime, timedelta, timezone
from email import message_from_bytes, policy
from email.message import EmailMessage

import agent_prompts as prompts
from base_loop import Loop
from harness_utils import (AGENT_FILES, FETCH_ERRORS, FETCH_TIMEOUT, addToContext, checkLength, describeError, formatItems, getFromContext, loadContext, readFeed,
                           retrieveContext, stripFences)
from messengers import sendMessage
from sources_library import ALL_NEWS_OUTLETS


INBOX_SEARCH_LIMIT = 10

EMAIL_PATTERN = r"[^@\s]+@[^@\s]+\.[^@\s]+"

DEFAULT_NEWS_OUTLETS = ("BBC World", "NPR News")


# ==============
# Email harness.
# Needs EMAIL_PASSWORD, EMAIL_SMTP_SERVER and (to read replies) EMAIL_IMAP_SERVER, from the settings of the loop or from the environment variables.
# If the first two are not set, the user is asked for them.
# ==============
# Tries the logins without sending anything, so a wrong password is found before the swarm runs. It returns "", or a sentence for the user.
def checkEmailLogin(sender, password, smtpServer, imapServer=""):
    try:
        with smtplib.SMTP(smtpServer, 587, timeout=FETCH_TIMEOUT) as server:
            server.starttls()
            server.login(sender, password)
    except smtplib.SMTPAuthenticationError:
        return f"{smtpServer} refused the address or the password. Providers like Gmail, Outlook and Yahoo need an app password, not the normal one."
    except (OSError, smtplib.SMTPException) as error:
        return f"Could not send through {smtpServer}: {error}"
    if imapServer:
        try:
            with imaplib.IMAP4_SSL(imapServer, timeout=FETCH_TIMEOUT) as inbox:
                inbox.login(sender, password)
        except (OSError, imaplib.IMAP4.error) as error:
            return f"Sending works, but {imapServer} could not be used to read the replies: {error}"
    return ""


class EmailLoop(Loop):
    def __init__(self, agent, sender, receiver, subject, request, language="English", numberOfLoops=5):
        super().__init__(agent, numberOfLoops, "EMAIL_RULES.md")
        self.sender = sender
        self.receiver = receiver
        self.subject = subject
        self.request = request
        self.language = language

    def fetchLatestEmail(self):
        server = self.settings.get("EMAIL_IMAP_SERVER") or os.environ.get("EMAIL_IMAP_SERVER")
        if not server:
            self.notifyUser("EMAIL_IMAP_SERVER is not set, so your inbox is not read. Writing a new email.")
            return ""
        with imaplib.IMAP4_SSL(server) as inbox:
            inbox.login(self.sender, self.getSetting("EMAIL_PASSWORD", secret=True))
            inbox.select("INBOX", readonly=True)
            status, found = inbox.search(None, "FROM", f'"{self.receiver}"')
            for emailId in reversed(found[0].split()[-INBOX_SEARCH_LIMIT:]):
                status, data = inbox.fetch(emailId, "(RFC822)")
                message = message_from_bytes(data[0][1], policy=policy.default)
                if self.subject.lower() in str(message["subject"]).lower():
                    body = message.get_body(preferencelist=("plain", "html"))
                    return body.get_content() if body else ""
        return ""

    def describe(self, draft):
        return f"To: {self.receiver}\nSubject: {self.subject}\n\n{draft}"

    def describeTask(self):
        return f"Write an email in {self.language} to {self.receiver} with the subject \"{self.subject}\" saying: {self.request}. It is sent from {self.sender} once the user approves it."

    def sendEmail(self, mail):
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = self.receiver
        message["Subject"] = self.subject
        message.set_content(mail)
        with smtplib.SMTP(self.getSetting("EMAIL_SMTP_SERVER"), 587) as server:
            server.starttls()
            server.login(self.sender, self.getSetting("EMAIL_PASSWORD", secret=True))
            server.send_message(message)

    def run(self):
        if not re.fullmatch(EMAIL_PATTERN, self.receiver):
            self.notifyUser(f"{self.receiver} is not a valid email address.")
            return None
        task = prompts.EMAIL_PROMPT.format(language=self.language, sender=self.sender, receiver=self.receiver, subject=self.subject,
                                           request=self.request, incoming=self.keepTrying(self.fetchLatestEmail) or "None, this is a new email.",
                                           sent=retrieveContext("email_contexts.json", [self.sender, self.receiver]))
        mail = self.reviewLoop(task, self.checkRules, key="mail")
        if mail is None:
            self.notifyUser("The email was not sent.")
            return None
        if self.sendOnce("sent", lambda: self.sendEmail(mail), "sending the email"):
            self.logAction(f"Sent the email \"{self.subject}\" to {self.receiver}.")
        addToContext("email_contexts.json", [self.sender, self.receiver, self.subject], mail)
        self.saveResult("sent_email", self.describe(mail), ".txt")
        self.notifyUser(f"Email sent to {self.receiver}.")
        return mail


# ==============
# Calendar harness.
# The events are kept in calendar_contexts.json and exported to calendar_events.ics,
# which can be imported into Google Calendar, Outlook, Apple Calendar, etc.
# ==============
class CalendarLoop(Loop):
    def __init__(self, agent, request, numberOfLoops=5):
        super().__init__(agent, numberOfLoops, "CALENDAR_RULES.md")
        self.request = request

    def describeTask(self):
        return f"Book this event in the calendar: {self.request}"

    def parseEvent(self, draft):
        event = json.loads(stripFences(draft))
        start = datetime.strptime(f"{event['date']} {event['time']}", "%Y-%m-%d %H:%M")
        return start, int(event["duration"]), str(event["subject"])

    def upcomingEvents(self):
        today = f"{datetime.now():%Y-%m-%d}"
        upcoming = {date: events for date, events in sorted(loadContext("calendar_contexts.json").items()) if date >= today}
        return json.dumps(upcoming, ensure_ascii=False) if upcoming else "None, the calendar is free."

    def checkEvent(self, draft):
        try:
            start, duration, subject = self.parseEvent(draft)
        except (ValueError, KeyError, TypeError):
            return f"Reply only with JSON like {prompts.EVENT_EXAMPLE}"
        if start < datetime.now() or duration <= 0:
            return "The event must start in the future and have a duration above 0 minutes."
        return ""

    def findOverlap(self, start, duration):
        end = start + timedelta(minutes=duration)
        for time, event in (getFromContext("calendar_contexts.json", [f"{start:%Y-%m-%d}"]) or {}).items():
            otherStart = datetime.strptime(f"{start:%Y-%m-%d} {time}", "%Y-%m-%d %H:%M")
            if start < otherStart + timedelta(minutes=event["duration"]) and otherStart < end:
                return f"'{event['subject']}' at {time} for {event['duration']} minutes"
        return ""

    # An overlap is not a mistake of the agent, so the user decides: book it anyway or choose another time.
    def describe(self, draft):
        try:
            start, duration, subject = self.parseEvent(draft)
        except (ValueError, KeyError, TypeError):
            return draft
        overlap = self.findOverlap(start, duration)
        warning = f"\nWarning, this overlaps with {overlap}. Type yes to book it anyway, or tell me another time." if overlap else ""
        return f"{subject}\n{start:%A %d %B %Y} at {start:%H:%M} for {duration} minutes{warning}"

    def exportCalendar(self):
        stamp = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//SwarmUP//Calendar//EN"]
        for date, events in loadContext("calendar_contexts.json").items():
            for time, event in events.items():
                start = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
                end = start + timedelta(minutes=event["duration"])
                # The .ics format needs backslashes, semicolons and commas to be escaped.
                subject = re.sub(r"([\\;,])", r"\\\1", event["subject"]).replace("\n", "\\n")
                lines += ["BEGIN:VEVENT", f"UID:{start:%Y%m%dT%H%M}@swarm-up", f"DTSTAMP:{stamp}",
                          f"DTSTART:{start:%Y%m%dT%H%M%S}", f"DTEND:{end:%Y%m%dT%H%M%S}", f"SUMMARY:{subject}", "END:VEVENT"]
        lines.append("END:VCALENDAR")
        for place in (AGENT_FILES, self.folder):
            if place:
                (place / "calendar_events.ics").write_text("\r\n".join(lines) + "\r\n", encoding="utf-8", newline="")

    def run(self):
        task = prompts.CALENDAR_PROMPT.format(today=f"{datetime.now():%A %Y-%m-%d %H:%M}", request=self.request,
                                              booked=self.upcomingEvents(), example=prompts.EVENT_EXAMPLE)
        event = self.reviewLoop(task, self.checkEvent, key="event")
        if event is None:
            self.notifyUser("The event was not booked.")
            return None
        start, duration, subject = self.parseEvent(event)
        addToContext("calendar_contexts.json", [f"{start:%Y-%m-%d}", f"{start:%H:%M}"], {"subject": subject, "duration": duration})
        self.exportCalendar()
        calendarFile = (self.folder or AGENT_FILES) / "calendar_events.ics"
        self.logAction(f"Booked \"{subject}\" on {start:%Y-%m-%d} at {start:%H:%M} for {duration} minutes, and wrote {calendarFile}.")
        self.notifyUser(f"Booked. Import {calendarFile} into your calendar app.")
        return self.describe(event)


# ==============
# News briefing harness.
# The outlets are names from NEWS_OUTLETS in sources_library.py (a drop list), or any web address the user writes.
# An outlet that fails is skipped with a message. Nothing is saved until the user approves the briefing.
# collectAt is the time of the day (HH:MM) when the feeds are collected. A swarm makes the agent wait for it, and without it
# the feeds are collected right away.
# ==============
# The next time the clock shows HH:MM: today if it is still to come, tomorrow otherwise.
def nextOccurrence(clock):
    try:
        hour, minute = (int(part) for part in clock.strip().split(":"))
        moment = datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
    except ValueError:
        raise ValueError(f"'{clock}' is not a time. Write it as HH:MM, like 07:30.") from None
    return moment if moment > datetime.now() else moment + timedelta(days=1)


class NewsLoop(Loop):
    # messenger is a messaging app of MESSENGERS (like Telegram) and messengerSettings what it needs (see MESSAGING_APPS in sources_library.py).
    # With them, the approved briefing is also sent to the phone of the user.
    def __init__(self, agent, topics="", outlets=DEFAULT_NEWS_OUTLETS, maxWords=250, language="English", numberOfLoops=5, collectAt=None,
                 messenger=None, messengerSettings=None):
        super().__init__(agent, numberOfLoops, "NEWS_BRIEFING_RULES.md")
        self.topics = topics
        self.outlets = outlets
        self.maxWords = maxWords
        self.language = language
        self.collectOn = collectAt if isinstance(collectAt, datetime) else nextOccurrence(collectAt) if collectAt else None
        self.messenger = messenger
        self.messengerSettings = dict(messengerSettings or {})

    def startTime(self):
        return self.collectOn

    def describeTask(self):
        schedule = f" The feeds are collected on {self.collectOn:%Y-%m-%d at %H:%M}." if self.collectOn else ""
        sending = f" The approved briefing is also sent to {self.messenger}." if self.messenger else ""
        return (f"Write a news briefing in {self.language} of at most {self.maxWords} words about {self.topics or 'anything important'}, "
                f"from the feeds of {', '.join(self.outlets)}.{schedule}{sending}")

    def fetchHeadlines(self):
        headlines, reached = [], 0
        for outlet in self.outlets:
            try:
                items = readFeed(ALL_NEWS_OUTLETS.get(outlet, outlet))
                if not items:
                    raise ValueError("it has no stories, so it is not a news feed")
            except FETCH_ERRORS as error:
                self.notifyUser(f"Skipping {outlet}: {describeError(error)}.")
                continue
            headlines += items
            reached += 1
        if not headlines:
            self.checkOnline("the news feeds were read")
        self.notifyUser(f"Read {len(headlines)} stories from {reached} of {len(self.outlets)} outlets.")
        return headlines

    # The briefing is sent only once, exactly as the user approved it.
    def deliver(self, briefing):
        send = lambda: sendMessage(self.messenger, self.messengerSettings, briefing)
        if self.messenger and self.sendOnce("delivered", send, f"sending the briefing to {self.messenger}"):
            self.logAction(f"Sent the news briefing to {self.messenger}.")
            self.notifyUser(f"The briefing was sent to {self.messenger}.")

    def run(self):
        now = datetime.now()
        date, time = f"{now:%Y-%m-%d}", f"{now:%H:%M}"
        headlines = self.keepTrying(self.fetchHeadlines)
        if not headlines:
            self.notifyUser("No headlines could be fetched, so there is no briefing.")
            return None
        task = prompts.NEWS_PROMPT.format(date=date, language=self.language, maxWords=self.maxWords, topics=self.topics or "anything important",
                                          earlier=retrieveContext("news_contexts.json", [date]), headlines=formatItems(headlines))
        briefing = self.reviewLoop(task, lambda draft: checkLength(draft, self.maxWords), key="briefing")
        if briefing is None:
            self.notifyUser("The briefing was not saved.")
            return None
        addToContext("news_contexts.json", [date, time], briefing)
        self.saveResult("news_briefing", briefing)
        self.logAction(f"Saved the news briefing of {date} at {time}.")
        self.notifyUser("The briefing is saved.")
        self.deliver(briefing)
        return briefing
