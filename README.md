# Orbit

A voice-first personal assistant: a LiveKit voice agent with a React PWA in front of it.

## Layout

| Folder | What it holds |
|---|---|
| `backend/` | Python. The voice agent (`src/agent.py`) and the HTTP API the app calls (`src/api/`). |
| `frontend/` | React + TypeScript PWA built with Vite. |

The two sides meet in two places only: the frontend calls the backend's `/api` routes, and both
use the agent name `orbit` (`backend/src/api/settings.py`, `frontend/src/voice/VoiceProvider.tsx`).

## What this Mac needs

| For | What |
|---|---|
| Everything | uv with Python 3.12, Node and npm, and `backend/.env.local` |
| Google | The OAuth client and token in `backend/.secrets/` (see [Google services](#google-services)) |
| Long-term memory | A Mem0 account, with its key as `MEM0_API_KEY` |
| Session memory | Redis, running (`brew services start redis`) |
| Mail reads and reply drafts | The Ollama app, running and signed in (`ollama signin`), with `gpt-oss:120b-cloud` |
| Browser | Google Chrome, and `npx` for Playwright's MCP server |
| Screenshots | The Screen Recording permission for the app the backend runs in |

Mem0, Redis and Ollama are all needed. Orbit still starts without one of them and carries on,
so check they are there: `redis-cli ping` prints `PONG`, `ollama list` shows
`gpt-oss:120b-cloud`, and Orbit answers "what do you remember about me?" from Mem0.

## Run it locally

Copy `backend/.env.example` to `backend/.env.local` and fill in `LIVEKIT_URL`,
`LIVEKIT_API_KEY` and `LIVEKIT_API_SECRET` from your LiveKit Cloud project, `GOOGLE_API_KEY`
for the Gemini models the agent uses, and `MEM0_API_KEY`. Then start three processes, each in
its own terminal:

```sh
# 1. The voice agent
cd backend && uv sync && uv run python src/agent.py dev

# 2. The API the frontend calls (issues LiveKit session tokens)
cd backend && uv run uvicorn api.app:app --reload --port 8000

# 3. The app
cd frontend && npm install && npm run dev
```

Open http://localhost:5173 and press the mic. The dev server proxies `/api` to port 8000.

To try the installable PWA build: `npm run build && npm run preview` in `frontend/`, then open
http://localhost:4173. The service worker is only active in that build, not in `npm run dev`.

## Google services

Gmail, Calendar, Chat, Drive and Sheets share one Google sign-in. Save the OAuth client JSON
from Google Cloud Console ("Desktop app" type) as `backend/.secrets/google-oauth-client.json`,
then from `backend/`:

```sh
uv run python -m integrations.google connect           # all five, read-only
uv run python -m integrations.google connect --write   # add sending, events, posting, documents, sheet changes
uv run python -m integrations.google status            # what is connected, and does each API answer
uv run python -m integrations.google disconnect
```

Tokens are stored in `backend/.secrets/google-token.json`, readable only by you and ignored
by git.

The agent can read all four (`backend/src/agent_tools/google.py`): look up and read mail,
list calendar events, fetch recent Chat messages, and find and read Drive files. In Drive
it can also list what is inside a folder, named by the user or reached from another listing;
with no folder named it lists the top level of My Drive. If several folders share the name,
it asks which one.

Every file and folder it finds in Drive comes with its link. On the card, a name opens the
file in a new tab and the button beside it copies its link; a folder's card, and a file
Orbit has opened, have "Open in Drive" and "Copy link" at the bottom. Ask for a link and Orbit
finds the file and points you to the card: it does not read the address out unless you ask.
A link it puts in an email or a document is the one Drive gave, written in full
(`whole_links` in `backend/src/agent_tools/actions.py` sees to that). A link opens for
whoever is already allowed to see the file: Orbit does not change who a file is shared with.

Orbit can also put a file itself on your screen. Ask it to show or open a file in Drive, or
a file attached to an email, and it arrives in a card of its own
(`frontend/src/cards/FileCard.tsx`). You can also press the eye beside a file in a Drive
list, or an attachment on an email's card.

- **A picture** is shown as a picture, and **a PDF** in the browser's own PDF viewer.
- **A text file** is shown as text. **A Markdown file** is drawn with its headings, lists,
  tables and code, and **an HTML file** is drawn as a page, without its styles, pictures
  and scripts.
- **A Google Doc or Slides file** is shown as a PDF of itself, and **a Google Sheet** as a
  table of its cells (see [Google Sheets](#google-sheets)).
- **Anything a browser cannot draw**, such as a Word document or a video, is shown by the
  picture Drive keeps of its first page, with a note saying so and the link to Drive.
- **An archive or another file with nothing to look at** is not shown: the card says so.

How each kind of file is shown is decided in one place (`backend/src/files.py`). The app
fetches the file from the backend (`backend/src/api/files.py`), which fetches it from Google.
Some limits:

- Orbit reads out a document's or a text file's words, as before. It cannot see a picture or
  read a PDF: it puts them on screen and says so. The tool tells it not to describe them.
- A file is somebody else's writing. A Markdown or an HTML file is not drawn from what it
  says itself but from clean HTML the backend makes of it (see
  [Writing with formatting](#writing-with-formatting)): nothing in it is run, and pictures
  it points to are not fetched. A link in it opens in a new tab.
- A file over 50 MB is not shown, and the card says so. Google makes no PDF of a very long
  Google Doc; the card then shows its text.
- The file passes through the backend on this machine and is not kept there. The browser
  may keep a picture or a PDF for five minutes.
- A phone's browser may not show a PDF inside a page: Chrome on Android cannot. The card
  then says so and offers to open the file. This has not been tried on a real phone.

In Chat it can also list your conversations and read one of them back in time: "what did
Asha send me last week", "what was said in the Design team space on Friday", then "go
further back". A space is found by its name. Google gives direct messages and unnamed
groups no name, so Orbit calls each by the people who write in it, which it works out from
the latest messages and keeps in `backend/.data/orbit.db`; the first look at your chats
takes a few seconds for that reason, and later ones do not. A person's name on its own
means your direct message with them. A chat where only you have written lately cannot be
found by name. Chat has no search by words: Orbit reads a stretch of a conversation and
answers from that.

Name a day or days and you get all of it: every message, up to 200, in the order written,
each shown in full on a card you can scroll. With no day named it is the latest 20. Ask
what someone said and Orbit gives the gist; ask it to read the messages and it reads every
one out, which for a busy day takes a few minutes, so interrupt it when you have heard
enough.

With write access (`connect --write`) it can also reply to an email, send a new one,
forward one, create a calendar event, answer an invitation, create a document in Drive (a
Google Doc, or a Markdown, HTML or text file if you ask for one), add text to the end of
a Google Doc it created (`backend/src/agent_tools/actions.py`), and add, change and delete
rows of a Google Sheet or create one: see [Google Sheets](#google-sheets). An email it sends
can carry files: see [Attachments](#attachments). In Drive it can only change
files it created itself; that is what the `drive.file` scope allows. Adding text to a
document goes through the Google Docs API, which must be enabled in your Cloud project;
creating one needs only the Drive API. Each of
these is drafted first and only carried out after you have heard the draft and then agreed;
the rule is enforced in `backend/src/actions/gate.py`, not left to the model. Drafts and
their outcomes are logged in `backend/.data/orbit.db`, and each draft appears as a card on
the home while it waits for your go-ahead.
Set `ORBIT_DRY_RUN=1` for the agent to go through the whole flow without sending anything.

## Google Sheets

Orbit reads Google Sheets and changes them (`backend/src/integrations/google/gsheets.py`;
`sheets_read` in `backend/src/agent_tools/google.py`, the `sheets_draft_` tools in
`backend/src/agent_tools/actions.py`).

Ask about a sheet by its name: "what did I spend on food in my expenses sheet", "read me
the October tab of the budget", "which tasks in the trip sheet are still open". Orbit finds
the spreadsheet, reads a tab of it (the one you name, else the one it read last, else the
first) and puts it on your screen as a table, with column letters along the top and row
numbers down the side as Sheets has them. The card's buttons show the other tabs. If
several spreadsheets fit the name, Orbit asks which one. A tab is read whole, down to row
10,000. Orbit takes in up to 500 of its rows at once and reads on when the answer may be
further down, and the card shows up to 2,000.

It can change a sheet in four ways. Each is drafted first and carried out only after you
have heard the draft and said yes, like an email:

- **Add rows** at the end of a tab: "add tea for 3.20 to my expenses sheet, under food".
  Each value goes under its header, and a column you gave nothing for is left alone. In a
  sheet with a date column, a day you did not name is today.
- **Change cells**: "change the amount for the train to 15", "mark book flights as done".
  The draft shows each cell as it is and as it will be, and says when the change would
  replace a formula.
- **Delete rows**: "delete the coffee entry". The rows below move up.
- **Create a spreadsheet**: "make a sheet called Workouts with columns date, exercise and
  reps", with rows in it if you give them. Its first row is frozen and bold.

The draft's card shows the change as a table. Once it is done, the sheet comes forward with
the rows or cells it changed marked. A value is written as if you had typed it: 4.50
becomes a number, 2026-10-15 a date, and a value that starts with = a formula. Orbit cannot
format, sort or filter a sheet, add a tab or a chart, or share one.

These are decided by code, not left to the model:

- Cells and rows that are already there are changed only if they still hold what the draft
  showed. If the sheet changed in between, or a row was put in above the one to be
  deleted, nothing is changed and Orbit says so.
- Orbit changes cells or deletes rows only in a sheet it has read in the same conversation,
  so a row number is never a guess. A change with no tab named goes to the tab it read last.
- A formula that fetches from the web whenever the sheet is opened (`IMPORTXML`,
  `IMPORTDATA`, `IMPORTHTML`, `IMPORTFEED`, `IMPORTRANGE`, `IMAGE`) is refused. What Orbit
  writes can be steered by what it has read, and such a formula could send the sheet's
  contents to whoever wrote it.
- When no cell or header shows a currency, Orbit is told to say amounts as plain numbers.
  Left to itself, it said every amount in dollars.

**Setting it up** takes two steps in Google, once:

1. Enable the Google Sheets API in your Google Cloud project (APIs & Services, Library,
   Google Sheets API). Until then Orbit says Sheets is not switched on, the Connections
   card says "API not enabled", and a sheet Orbit opens from Drive is shown as before: as
   a PDF, with the text of its first tab for Orbit to read. A sheet's card that cannot
   get its cells offers to show its pages instead.
2. Run `uv run python -m integrations.google connect --write` again from `backend/`.
   Reading needs only the Drive access Orbit already has. Changing a sheet needs Google's
   `spreadsheets` permission, and unlike Drive's, where Orbit can change only the files it
   created, that permission covers every spreadsheet in your account. Creating a new
   spreadsheet needs only Drive's.

How far this has been tried: reading, drafting and carrying out each kind of change were
tested against a stand-in for the Sheets API, and with the real model talking to it. Against
the real account, only finding spreadsheets, the fallback for a sheet opened from Drive and
the message for an API that is not enabled were checked, because the Sheets API is not
enabled in the Cloud project yet. Nothing has been written to a real sheet.

## Screenshots

Ask Orbit to look at your screen and it takes a screenshot, once, and tells you what is
on it (`backend/src/agent_tools/screen.py`): "take a screenshot and tell me what the
error says", "what's on my screen", "read this error for me". It takes a picture of each
of your screens, puts it on your screen as a card so that you see what it looked at, and
asks Gemini what the picture shows. A further question about it, such as "and the line
above it?", is asked of the screenshot already taken. Nothing is watched between
screenshots: Orbit sees your screen only at the moment it takes one.

Name a window and only that window is taken: "take a screenshot of my browser window",
"look at the Chrome window", "check the GitHub page", "what's in VS Code?". Orbit finds it
by its app, by "the browser" or "the web page" for a browser's window, or by words of its
title, and takes the frontmost that fits. Orbit's own window is never the one meant.

- macOS is asked for the window by itself, so nothing around it is in the picture: no
  other window, not even one over it, and no other screen. Where macOS will not give a
  window by itself, the part of the screen it covers is taken instead, with whatever is
  in front of it, and Orbit is told so.
- The window has to be on the screen. One on another desktop (a Space, as when another
  app is full screen), or minimised, cannot be taken: Orbit says which apps have a window
  on the screen.

- Orbit takes a screenshot only when your own words ask it to look at your screen, or to
  read or check something in front of you. That is decided by code, not by the model.
  When it is not sure, it asks you first, and your yes lets it take one. The words that
  asked for one screenshot do not ask for the next.
- Every screen is in the picture, and whatever is on them, notifications included, goes to
  Google to be read. Put away what you would not want read before you ask.
- Anything written on your screen is treated as information to report, never as an
  instruction to follow.
- Nothing is kept on disk. The pictures pass through a private temporary folder that is
  deleted at once, are held for the rest of the conversation for further questions, and
  go with it.
- Nothing can be taken while the Mac's screen is locked, and Orbit says so.
- It works on a Mac, through macOS's own `screencapture` and `sips`, while the backend
  runs on the computer whose screen it is. macOS has to allow Screen Recording for the app
  the backend runs in (System Settings, Privacy & Security, Screen & System Audio
  Recording). Visual Studio Code, which the backend runs in here, already had it. Without
  it Orbit says what to switch on, and macOS asks once.
- Reading a screenshot takes Gemini about four seconds. It is one request to
  `gemini-3.5-flash` on the same Google key as the voice, with one picture for each
  screen, so it is free as far as your Gemini plan's free allowance goes. Set
  `ORBIT_VISION_MODEL` in `backend/.env.local` for another model. `gemini-3.8-flash`,
  tried first, gave no answer within 30 seconds.

How far this has been tried: reading was tried for real, on a made-up screen with an
error on it, which Gemini read word for word. Taking the screenshot was tried with
stand-ins for macOS's commands, and the commands themselves on one pixel of the screen.
No screenshot of the whole screen has been taken. Listing the windows and noticing a
locked screen were tried on this Mac. Taking one window was not: the screen had locked
by then, and a locked screen refuses every screenshot.

## Writing with formatting

Orbit writes plain text unless what it writes needs more. When an email, a document or an
answer has a list, a table, headings or bold text, or when you ask for Markdown or HTML, it
writes in Markdown or HTML (`backend/src/writing.py`).

- **An email**, such as "email Asha the packing list as bullet points", goes out formatted,
  with its plain words beside it for mail programs that show no formatting.
- **A document**, such as "make a doc with a table of the costs", becomes a Google Doc that
  keeps its headings, lists and tables. Ask for a Markdown file or an HTML file and it is
  saved as `name.md` or `name.html` instead, as written.
- **An answer with a shape**, such as "compare the train and the flight in a table" or
  "give me a checklist", is put on your screen as a card, and Orbit says the gist aloud. A
  short answer is only spoken.

Whichever of the two Orbit writes in, it is turned into one clean kind of HTML: headings,
paragraphs, lists, tables, links, emphasis, code and quotes. Scripts, styles, forms and
pictures fetched from elsewhere are taken out, and with the styles goes anything that could
hide words from you. That matters because what Orbit writes can be steered by what it has
read, in an email or on a web page.

- The draft's card is drawn from the same HTML that the email carries or the document is
  made from, so what you see is what is sent or saved. Each link in a draft has its address
  written beside it.
- Orbit reads a formatted draft out as its words, without the symbols. The rule that
  nothing happens before you have heard the draft and agreed is unchanged.
- The app does not trust that HTML either: it draws only the same few kinds of tag
  (`frontend/src/components/Formatted.tsx`), so nothing else can reach the page.
- An HTML file Orbit creates has structure and no styling of its own. Text added to the end
  of an existing Google Doc is still plain.
- Sending a formatted email and creating a formatted document were tried with stand-ins
  for Gmail and Drive and in dry-run mode (`ORBIT_DRY_RUN=1`), not by sending real mail.

## Attachments

An email Orbit sends can carry files (`backend/src/attachments.py`,
`backend/src/agent_tools/attaching.py`). In a conversation you ask and Orbit puts them on
its draft. On a suggested reply you add them yourself.

- **From Drive:** "email Asha the Q3 budget from my Drive as an attachment". A file with
  bytes of its own goes as it is. A Google Doc, Sheet or Slides file goes as a PDF, or as a
  Word, Excel or PowerPoint file if you ask for one. Google makes no PDF of a very long
  Google Doc: that one goes as a Word file, and Orbit says so.
- **From another email:** "send the invoice from Asha's email to Ravi". Or "forward Asha's
  email to Ravi", which passes the whole email on with every file it came with.
- **From your computer:** "email Asha the signed contract from my desktop". Orbit opens an
  Upload card that says what it is asking for. Press "Choose a file" or drop the file on
  it, and Orbit is told the file is there and drafts the email. It asks the same way when
  a file you want sent is not in your Drive or your email. A browser lets a page open the
  computer's file picker only on a press, which is why the card has a button and Orbit
  cannot open the picker by itself. You can also drop a file anywhere on Orbit at any
  time: it waits on the "Ready to attach" card until you tell Orbit to send it.
- **Written by Orbit:** "write the packing list as a text file and attach it". The file is
  text, Markdown, HTML or CSV, and waits on the same card.

Each file is copied into `backend/.data/attachments/` the moment it is put on a draft, and
the email is built from that copy. So what goes out is what you were shown, whatever
happens to the original in between. Orbit names each attachment when it reads the draft
out. The draft's card lists them, and one the app can show opens from there. The copies
are deleted when the email is sent, cancelled or left to expire. A file you added is
deleted once an email has carried it, or after a day.

Gmail takes 25 MB of files on one email. Orbit checks before it drafts, says when a file is
too large, and offers to put its Drive link in the email instead. It does not change who a
file is shared with.

A file is how something leaves your account for good, and what Orbit drafts can be steered
by what it has read. So whether a file may go on a draft is decided by code, from what you
said, not by the model (`asked_for` in `backend/src/agent_tools/attaching.py`):

- Your own words must ask for it: a word such as attach or include, or a word such as send
  together with the file's name. For a file you added yourself, send is enough, and picking
  a file on the Upload card counts as asking for it to be attached. An email is passed on
  when you say forward, or send it on.
- Words that put files on one draft do not put files on the next.
- Whether a Google file goes as a PDF or as a Word, Excel or PowerPoint file is decided the
  same way, from whether you asked for one of those. Left to itself, the model chose Excel
  for a sheet about half the time.
- Failing that, Orbit is sent to ask you, and a file goes on only after it has spoken to
  you of attaching and you have not said no.
- A draft you ask to change keeps its files, as long as it still goes to the same people.
- What the Upload card says it is asking for is the model's wording. Give it a file only
  for something you asked for. The draft that follows is read out and waits for your yes
  like any other, and a draft made before you have picked the file is refused.

This is a check of words, and it can be wrong both ways. Whatever it lets through still has
to be read out and agreed to, as every draft does.

**Finish in Gmail.** For what Orbit cannot do, such as a picture inside the text, or a file
over the limit shared from Drive, say "put it in my Gmail drafts" or press "Finish in
Gmail" on the draft. The email is saved in your Gmail drafts with its files, nothing is
sent, and the card then opens it in Gmail.

How far this has been tried: fetching files from Drive and from an email was checked
against a real account, without changing anything in it. Sending an email with files,
the upload that large emails go by, and saving to the Gmail drafts were tried with
stand-ins for Gmail and in dry-run mode, not with real mail. Where the "Open in Gmail"
link lands has not been tried either: if Gmail does not open the draft itself, it opens
the drafts folder, where the draft is the newest.

## Reply drafts

Orbit reads the mail waiting on you and suggests replies (`backend/src/drafting/`). It looks at
unread mail Gmail marks important and at inbox mail from the last 14 days, says in one line
what each email asks of you, and drafts a reply where one is needed. Drafts appear on the
"Needs you" card, and each opens as its own card where you can edit, send or discard it. Nothing is sent
until you press Send.

A reply can carry files from your computer: press "Attach a file" on its card, or drop
them on the card. Each is listed under the reply, opens from there, and can be removed.
Send goes out with exactly the files on the card: if they have changed in another window
since, nothing is sent and the card says so. "Finish in Gmail" puts the reply in your Gmail
drafts, with its files, for anything Orbit cannot do.

The reading is done by `gpt-oss:120b-cloud` through Ollama, so the Ollama app must be running
on this machine and signed in (`ollama signin`). To call Ollama's cloud directly instead, set
`OLLAMA_API_KEY` in `backend/.env.local`. `ORBIT_DRAFT_MODEL` picks a different model. The text
of the emails being read is sent to Ollama's cloud.

## Memory

Orbit remembers things across conversations with [Mem0](https://mem0.ai), reached through
its hosted MCP server and [LiveKit's MCP support](https://docs.livekit.io/agents/logic/tools/mcp/)
(`backend/src/agent_tools/memory.py`). Put a key from the Mem0 dashboard in
`backend/.env.local` as `MEM0_API_KEY`. Without it Orbit works as before and says it has no
long-term memory.

When a conversation starts, Orbit reads what Mem0 holds (up to 30 memories) into its
instructions, so it knows you without a lookup on every question. During a conversation it
saves lasting facts and preferences you mention, searches when something might depend on an
earlier conversation, and corrects or forgets a memory when you ask. It is told never to save
passwords, PINs or card numbers, and not to save what it reads in your mail or files unless
you ask.

Memories are stored in your Mem0 account, not on this machine; you can see and delete them
in the Mem0 dashboard. Mem0 files a new memory in the background, so it takes a few seconds
to become searchable.

## Session memory

Orbit remembers what was said in a session until you close it
(`backend/src/session_memory.py`). A session is Orbit open in one browser tab, from opening
the app until the tab is closed. Put Orbit on standby and wake it again, and it still knows
what you told it before; a reload of the tab keeps the session too. A new tab is a new
session. This is not the long-term memory above: that is Mem0, and lasts across sessions.

- Everything you and Orbit say is kept in Redis under the session, and each new
  conversation starts with it as its history. Gemini takes it as what was said before, not
  as instructions. Of a very long session the newest 60,000 characters are given, and Orbit
  is told that the beginning was left out.
- The app tells the backend every few minutes that it is still open
  (`frontend/src/lib/appSession.ts`, `backend/src/api/session.py`). When its tab closes,
  what was said is let go a minute later. If the browser quits without saying so, it is let
  go 15 minutes after the app was last heard from.
- Words said in an earlier conversation do not count as asking for something now: the
  checks for what you asked for (attachments, screenshots, the browser) look only at the
  conversation in hand.
- Redis is reached at `REDIS_URL` in `backend/.env.local`, `redis://localhost:6379/0` if it
  is not set, and Orbit's keys start with `orbit:session:`. Here Redis runs from Homebrew
  (`brew services start redis`). Without Redis, Orbit works as before and remembers a
  conversation only while it lasts.
- What is kept is everything said, which can include what Orbit read out from your mail or
  your chats. Redis holds it in memory, but Homebrew's Redis also saves snapshots to
  `/opt/homebrew/var/db/redis/dump.rdb` (the `save` line of
  `/opt/homebrew/etc/redis.conf`), so it can reach the disk while a session lasts. Set
  `save ""` there to keep Redis in memory only.

How far this has been tried: keeping, reading back and letting go were tested with a
stand-in for Redis, and once through the Redis on this Mac. The app's signals were tried in
Chrome: a reload keeps the session, a new tab gets its own, and a closing tab says so. With
the real model, a flight time said before standby was known in the next conversation, 3
times out of 3.

## Browser

Orbit can drive a web browser with [Playwright's MCP server](https://github.com/microsoft/playwright-mcp):
open pages, read them, click, type and fill in forms (`backend/src/agent_tools/browser.py`).
It needs Node.js and Google Chrome on this machine; `npx` fetches the server the first time.

Before it first uses the browser in a conversation, Orbit asks whether you want to see it,
and starts it shown or hidden accordingly. "View mode", "watch mode" and "on screen" all mean
shown. Ask later to see it, or to hide it, and Orbit restarts that browser the other way and
opens its tabs again, from the start. The browser tools refuse to work until a browser has
been started.

When to ask is decided by the tool, not left to the model
(`backend/src/agent_tools/browser.py`). The model starts a browser without saying how unless
you have said; for the first browser of a conversation the tool then tells it to ask you.
When it does pass on a choice, it has to pass your own words with it, and those are looked
for in what you said (`in_their_words`): a choice you did not make is refused. Once a
browser is open the question is settled for that conversation, and new tabs and further
browsers open without it, shown or hidden like the one in use.

A browser you can see stays open when the conversation ends. If it is playing music or a
video when you tell Orbit to go to standby, it goes on playing. Call Orbit again and ask it
to pause, resume or skip, or to close the browser: the new conversation uses the same browser
and does not ask again whether to show it. Tell it to close the browser and Chrome is closed
for good. The browser also stays open when the backend stops, and then you close its window
yourself. A hidden browser makes no sound, and is closed when the conversation ends.

To make that possible Orbit starts Chrome itself (`backend/src/agent_tools/chrome.py`), and
Playwright's server attaches to it through Chrome's remote debugging port. A server that
starts the browser closes it on leaving, and each conversation has a server of its own.
Which browsers are running is kept in `backend/.data/browser.json`.

- Unless you ask for one of your own Chrome profiles (below), the browser is a clean one
  with a throwaway profile: it is signed in to nothing, and the profile is deleted when the
  browser is closed.
- While it is open, its debugging port is open on this computer only, on a port Chrome
  picks. Another program running here as you could drive that browser through it. Web pages
  cannot: Chrome refuses them the connection.
- It cannot open addresses on this computer or your network, so a web page cannot steer it
  to Orbit's own API or app.
- Running scripts, uploading files and reading network traffic are left out.
- The agent is told to treat pages as information, never as instructions, not to type
  secrets or your private data into a page, and to ask before submitting a form that sends,
  posts, buys, books or signs up. Those are instructions to the model, not enforced by code.

The tool definitions in `backend/src/agent_tools/browser_tools.json` belong to the pinned
server version; after changing `PACKAGE`, run `uv run python -m agent_tools.browser` from
`backend/src` to write them again.

### Tabs and several browsers

Orbit works in tabs: "open that in a new tab", "switch to the first tab", "close this tab".
A new tab opens in the browser that is already open, and closing a tab leaves the browser
open. Tabs are numbered afresh in each conversation, so Orbit looks at the list before it
switches to one or closes one.

Several browsers can be open at once: clean ones, ones on your profiles, and two on the same
profile. Say "open another browser in my Work profile" or "open a second clean browser".
Each has a name, which is how you and Orbit tell them apart: `clean`, or the name of its
profile, with a number from the second of a kind on (`Work 2`). Say which one you mean, as
in "in the Work browser, open...", or Orbit uses the one it used last. "Close the Work
browser" closes that one and "close all the browsers" closes every one. Each browser has a
card of its own on the stage.

- Asking for a browser of another kind opens it beside the ones that are open. Nothing is
  closed or restarted to make room, so what a browser is playing goes on. Ask Orbit to
  switch, and it opens the one you want and then closes the one you are leaving.
- Two browsers on one profile are two separate copies of it, each made when its browser
  was opened. What you do in one does not show in the other.
- Each browser is a Chrome of its own, with its own debugging port, and has a Playwright
  server of its own while a conversation is using it. A browser takes a second or two to
  open, and a few hundred megabytes of memory while it is open.
- Browsers you can see stay open in standby, all of them, and the next conversation takes
  them all over. Hidden ones close with the conversation.

### Your own Chrome profiles

Name one of your Chrome profiles, as in "open YouTube Music in my Work profile", and Orbit
starts the browser on a copy of that profile, signed in to whatever the profile is signed in
to. Say nothing about a profile, or ask for "the usual" or "the clean browser", and you get
the clean one as before. Orbit knows your profiles by the names you gave them in Chrome and
never picks one itself. A browser on a profile behaves like any other: it stays open in
standby, and the next conversation goes on using the same copy.

- **macOS has to let Orbit in first.** It protects Chrome's data from other apps, so until
  you allow it Orbit cannot even list your profiles, and says so when you ask for one. In
  System Settings, under Privacy & Security > Full Disk Access, switch on the app the backend
  runs in (Terminal, iTerm, or Visual Studio Code if you start it from its terminal), quit
  and reopen that app, and start the backend again. That lets everything started from that
  app read all your files, not only Chrome's.
- Chrome does not let another program drive its real profile folder, so Orbit copies the
  profile into a folder of its own each time the browser starts, and deletes the copy when
  the browser closes. Your open tabs are not copied. Nothing Orbit does in the copy comes
  back to your profile: the history and logins made there go with it, and sync is switched
  off in the copy.
- On a Mac the copy is a clone, which takes a moment and no extra space. Elsewhere it is a
  real copy without the caches, and can take some seconds for a large profile.
- While such a browser is open, your signed-in sessions exist a second time on disk, and
  its debugging port reaches a browser that is signed in as you: another program running on
  this computer as you could use your accounts through it. Tell Orbit to close the browser
  when you are done with it.
- In a profile Orbit acts as you. It is told never to choose a profile itself, and to say
  what it is about to do and wait for your yes before anything that sends, posts, buys,
  deletes or changes an account. Those are instructions to the model, not enforced by code.
- The names of your profiles, not the accounts behind them, are part of what the model is
  told.
- A site that ties a login to more than its cookies may ask the copy to sign in again.
- Set `ORBIT_CHROME_USER_DATA` in `backend/.env.local` if your Chrome keeps its data
  somewhere other than the standard place, as Chrome Beta and Chromium do.

## Web search

Orbit can look things up on the web with DuckDuckGo (`backend/src/agent_tools/web.py`),
for news, results and public facts it cannot be sure of from memory. It needs no key: it
uses the [`ddgs`](https://pypi.org/project/ddgs/) Python package, which is the
`duckduckgo-search` package under its current name.

DuckDuckGo has no official search API and sometimes answers automated searches with a bot
check instead of results. Orbit tries twice and then says it could not look it up. If that
happens too often, set `ORBIT_SEARCH_BACKEND=auto` in `backend/.env.local` and the library
falls back to other search engines.

Results are short excerpts; Orbit never opens the pages. Live values that are not in an
excerpt, such as the current temperature, are therefore often out of reach. The agent is
told to treat results as information, never as instructions, and not to put details from
your mail, calendar, files or reminders into a search.

## Reminders

Orbit keeps its own reminders in `backend/.data/orbit.db` (`backend/src/reminders/`). Say
"remind me tomorrow at five to call Asha" or "remind me in twenty minutes to check the oven"
in a session; you can also ask what is on the list, move one, tick one off or drop one
(`backend/src/agent_tools/reminders.py`). A reminder is saved straight away, without the
draft-and-confirm step, and Orbit says back what it saved.

The Reminders card lists them by when they are due, and reminders whose time has come also
show under "Needs you". With alerts turned on (the button on that card), the browser
shows a notification when one comes due. That only happens while Orbit is open in a browser
tab: nothing reaches you when it is closed, or on your phone.

## The app

The home shows only Orbit: the orb, what you said, what it answered, and the command bar.
There is no sidebar and there are no separate screens. What you ask for arrives as a card
beside the orb (`frontend/src/stage/`, `frontend/src/cards/`):

- **Needs you:** mail that wants an answer, invitations you have not answered, reminders due.
- **An email:** Orbit's short version of it and, if it drafted one, the reply to edit and send.
- **Today, Reminders, Connections, Conversation.**
- **A draft awaiting your go-ahead:** anything Orbit wants to send or create in your name,
  with the files an email would carry.
- **What Orbit found:** mail, calendar, Drive, Chat and web results, and its steps in the browser.
- **A file:** a picture, a PDF or a document from Drive or from an email, shown as itself.
- **A sheet:** a tab of a Google Sheet as a table, with what a change just did marked.
- **A screenshot:** what Orbit looked at when you asked it to look at your screen.
- **A note:** something Orbit wrote for the screen instead of saying it all: a table, a
  checklist, steps to follow.
- **Upload:** Orbit asking for a file that is on your computer.
- **Ready to attach:** the files you dropped on Orbit, and the ones Orbit wrote, waiting to
  go on an email.

Drag a card by its header. The minus button, or Esc for the front card, sends it to the
"In background" shelf above the command bar; click it there to bring it back.
You can also ask Orbit to do it: "clear the stage", "bring the reminders back", "close the
reminders card". The agent calls a function the app registers over LiveKit RPC
(`orbit.cards`), and the app answers with what it changed and which cards are now open.

The app fits the window it is in. Which of three layouts it uses follows the size of the
window (`frontend/src/stage/layout.ts`), and the styles follow that
(`frontend/src/styles/stage.css`):

- **A desktop-sized window**, at least 900px wide and 500px high: cards are windows around
  the orb, as described above.
- **A narrower window, a tablet or a phone held upright:** one card at a time. Orbit moves
  into a strip along the top and the card takes the space below it.
- **A phone on its side**, or any window under 500px high: one card at a time, to the right
  of Orbit.

With one card at a time, opening a card or bringing one back sends the card in front to the
shelf, which is then a single line that scrolls sideways, and a card cannot be dragged. Make
a window too small for windows and the card in front stays while the rest go to the shelf;
each keeps its place for when the window is large again, but comes back only when you ask
for it. If Orbit is asked to bring back several cards where one fits, the app tells it that
only the last came forward. On a touch screen the buttons are larger. Below 320 by 400
pixels (560 by 280 for a window on its side) the page scrolls instead of shrinking further.

Tell Orbit "go to standby", "go to sleep", "stop listening" or "bye" and it says a few
words and ends the conversation, which puts the app back in Standby; press the orb to talk
again. The agent uses LiveKit's prebuilt `EndCallTool` for this
(`backend/src/agent_tools/standby.py`), which closes the session and deletes the room. It
first sends the app a `standby` message on `orbit.stage`, so the app treats the agent leaving
as the end of the conversation and not as a lost connection. A browser on your screen is
left open, so what it is playing goes on in standby (see [Browser](#browser)).

In standby the app listens for its name: say "Orbit", "Hey Orbit", "OK Orbit" or "Wake up,
Orbit" and a conversation starts, as if you had pressed the orb
(`frontend/src/voice/wake.ts`). The name has to come first: "the moon is in orbit" does not
wake it. How it works, and its limits:

- The browser's own speech recognition does the listening, on your computer
  (`processLocally`), so nothing said in standby is sent anywhere. This needs a recent
  Chrome or Edge; the first time, the browser downloads its English speech pack. In a browser
  that cannot do this there is no wake word, and the orb works as before.
- It starts only after the microphone has been allowed, which happens the first time you
  press the orb. While it listens, the browser's microphone indicator stays on.
- With Orbit open in several tabs, only one of them listens.
- It answers to anyone who says the name, not only to your voice.
- It wakes only once the name has stayed heard for a moment, because the recogniser's first
  guess at a word is often revised. The recogniser is not told to expect "Orbit" either: with
  that hint it heard the name in whatever was said, and Orbit woke at anything.
- The short forms can be missed in some accents. With US and British test voices every form
  worked; with an Indian-English test voice only "Wake up, Orbit" worked every time.
- Wait for the orb to show Listening before you go on: what you say in the half second it
  takes to start is not heard.
- Set `VITE_WAKE_WORD=off` in `frontend/.env.local` to keep the microphone closed in standby.

Orbit should not hear what the computer itself is playing. Both microphones, the one for the
conversation and the one that listens for the name, are opened with echo cancellation set to
`all` (`frontend/src/voice/microphone.ts`), which asks the browser to remove everything this
computer plays from what the microphone picks up. The browser's default only has to remove
Orbit's own voice, so music or a video in another tab would reach Orbit as speech. Headphones
avoid the problem altogether.

Orbit answers once you have been silent for a second and a half, so a pause for thought in the
middle of a sentence is not taken for its end. Gemini's own wait is a little under a second,
and with it Orbit started to answer half of what was being said. The wait is part of every
answer: Orbit begins to reply a little under two seconds after you stop, where it took about
1.3 seconds before. To change it, set `ORBIT_SILENCE_SECONDS` in `backend/.env.local` and
restart the agent: `2` if Orbit still comes in while you are thinking, `1.2` if it feels slow.
A pause longer than the wait is still taken for the end, because only the silence is judged,
not whether the sentence sounds finished. LiveKit's turn detector does judge that, but with
this Gemini model it gave doubled and broken-off answers, so it is not used
(`backend/src/agent.py`). `backend/tests/test_turn_taking.py` speaks a recorded question with
a pause in it to the real model and checks that Orbit waits for the rest.

Cards open in two ways. During a conversation the agent asks for them: a tool that has
something to show publishes a small message on the LiveKit text-stream topic `orbit.stage`
(`backend/src/agent_tools/stage.py`), and the app opens or updates the card. Without a
conversation, the three suggestions under the orb open their cards straight from the API.

Slack is not connected.

## Before hosting it anywhere

- `POST /api/token` has no authentication. Anyone who can reach it can start an agent session
  on your LiveKit account. Keep the API on localhost until a login sits in front of it.
- No other route has any either. Anyone who can reach the API can read the mail it lists
  and any of your spreadsheets, send a suggested reply, and put files of up to 25 MB each
  into `backend/.data/attachments/`.
- Set `ORBIT_ALLOWED_ORIGINS` on the API (comma-separated) and `VITE_API_BASE_URL` in the
  frontend when the two are served from different origins.
- The layout fits a phone, but a browser gives a page the microphone only on `https` or
  `localhost`. To talk to Orbit from a phone, the app has to be served over HTTPS.
- The phone and tablet layouts have been checked in a desktop browser at phone and tablet
  sizes, not yet on a real device.
