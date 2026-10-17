# Orbit

A voice-first personal assistant built with LiveKit, Gemini, and a React PWA.

Talk to Orbit to manage email, check your calendar, work with Google Drive and Sheets,
search the web, and set reminders. Results, files, and drafts appear as cards alongside
the conversation.

Orbit currently runs as a local application. The API does not include authentication;
see [Deployment and data](#deployment-and-data) before exposing it to a network.

## Features

- **Voice conversations:** real-time audio, interruptible responses, standby mode, and
  an optional wake word using the browser's local speech recognition.
- **Google services:** read Gmail, Calendar, Chat, Drive, and Sheets. Draft emails,
  create events and documents, respond to invitations, and update spreadsheets.
- **Files and attachments:** preview Drive files and email attachments, upload local
  files, and attach files to emails. Save drafts to Gmail to finish editing there.
- **Memory:** retain conversation history within a browser session using Redis and
  remember preferences across sessions using Mem0.
- **Browser automation:** navigate pages, manage tabs, and fill forms through Chrome
  and Playwright MCP, with visible or headless browsers.
- **Screen understanding:** capture a screen or named window on request and ask
  follow-up questions about it. Requires macOS.
- **Reminders and search:** manage local reminders with browser notifications and
  search the web without a search API key.

Google actions requested by voice are drafted and read back before execution. A code-level
approval gate requires confirmation in a later turn. Suggested email replies can also be
reviewed, edited, and sent from their cards.

## Getting started

### Prerequisites

- Python 3.12 and `uv`.
- Node.js 22.12+ and npm.
- A LiveKit Cloud project.
- A Google API key with access to the Gemini models configured in
  [the voice agent](backend/src/agent.py).

Additional services are needed for their respective features:

| Feature | Requirement |
| --- | --- |
| Google services | A Google Cloud OAuth client and the relevant APIs enabled |
| Long-term memory | A Mem0 account and `MEM0_API_KEY` |
| Session history across standby and reloads | A running Redis server |
| Suggested email replies | Ollama, signed in with access to the drafting model, or `OLLAMA_API_KEY` |
| Browser automation | Google Chrome; `npx` downloads the Playwright MCP server on first use |
| Screenshots | macOS with Screen Recording permission for the application running the backend |

Orbit can start without Mem0, Redis, or Ollama, but the corresponding features will be
unavailable.

### Configure and install

From the repository root:

```sh
cp backend/.env.example backend/.env.local
```

Set these values in `backend/.env.local`:

```dotenv
LIVEKIT_URL=wss://your-project.livekit.cloud
LIVEKIT_API_KEY=your-livekit-api-key
LIVEKIT_API_SECRET=your-livekit-api-secret
GOOGLE_API_KEY=your-google-api-key
```

Add `MEM0_API_KEY` if you want long-term memory. See
[Integrations](#integrations) for the other services.

Install dependencies:

```sh
(cd backend && uv sync)
(cd frontend && npm ci)
```

### Start the application

Run each process in a separate terminal, starting from the repository root.

**Voice agent**

```sh
cd backend
uv run python src/agent.py dev
```

**HTTP API**

```sh
cd backend
uv run uvicorn api.app:app --reload --port 8000
```

**Frontend**

```sh
cd frontend
npm run dev
```

Open [localhost:5173](http://localhost:5173), press the orb, and allow microphone access.
The Vite development server proxies `/api` requests to the backend on port 8000.

To preview the installable PWA, run `npm run build` followed by `npm run preview` in
`frontend/`, then open [localhost:4173](http://localhost:4173). The service worker is active
in the production build only.

## Integrations

### Google services

1. Enable the Google APIs you intend to use: Gmail, Calendar, Chat, Drive, and Sheets.
   Enable the Google Docs API to append text to documents created by Orbit.
2. Configure the OAuth consent screen and create an OAuth client of type **Desktop app**.
3. Save the client JSON as `backend/.secrets/google-oauth-client.json`.
4. From `backend/`, connect your account:

   ```sh
   uv run python -m integrations.google connect
   ```

The default connection requests read-only access. To enable sending email, creating
events and documents, and changing spreadsheets, request write access:

```sh
uv run python -m integrations.google connect --write
```

Check the connection from the same directory:

```sh
uv run python -m integrations.google status
```

To revoke access and remove the saved tokens, run
`uv run python -m integrations.google disconnect`.

Tokens are stored in `backend/.secrets/google-token.json`. Both the client file and tokens
are ignored by Git.

Drive writes are limited to files Orbit creates. Sheets write access covers all spreadsheets
the connected account can edit. Cell updates and row deletions require the sheet to have
been read in the current conversation, and execution checks that the affected values still
match the draft.

Sheets supports reading tabs, appending rows, updating cells, deleting rows, and creating
spreadsheets. Formatting, sorting, filtering, adding tabs, and sharing are not supported.
Google Chat currently supports reading conversations; posting messages is not implemented.

### Memory

- **Long-term memory:** set `MEM0_API_KEY` in `backend/.env.local`. Orbit uses Mem0's hosted
  MCP server to save, find, update, and forget memories across conversations. Memories can
  also be managed in the Mem0 dashboard.
- **Session memory:** start Redis and set `REDIS_URL` if it differs from
  `redis://localhost:6379/0`. Check the connection with `redis-cli ping`. History survives
  standby and reloads within a tab; a new tab starts a separate session. History expires
  shortly after the tab closes, or after 15 minutes without a heartbeat.

### Suggested email replies

Run Ollama and sign in with `ollama signin`. The default drafting model is
`gpt-oss:120b-cloud`. Alternatively, set `OLLAMA_API_KEY` to call Ollama's cloud directly;
that connection defaults to `gpt-oss:120b`.

Use `OLLAMA_HOST` to change the local endpoint or `ORBIT_DRAFT_MODEL` to choose another
model. With the default models, email content is processed in Ollama's cloud even when
accessed through the local Ollama application. Replies wait for review and a press of Send.

### Browser and screenshots

Browser automation requires Chrome on the backend host. Orbit asks whether to show or
hide the browser before first use. Visible browsers remain open in standby and can be
reused in the next conversation; headless browsers close when the conversation ends.

Browsers use a temporary, signed-out profile unless you explicitly name one of your Chrome
profiles. Named profiles are copied for the browser and the copy is deleted when the browser
closes. On macOS, accessing Chrome profiles may require Full Disk Access for the application
running the backend. A copied profile retains signed-in sessions, so close it when finished.

Screenshots work only on macOS and capture the computer running the backend. Enable Screen
Recording for its terminal or editor under **System Settings > Privacy & Security**.
Orbit captures only when requested and sends the image to Gemini for analysis. Images are
held for follow-up questions during the conversation; temporary capture files are deleted.

## Configuration

See [backend/.env.example](backend/.env.example) and
[frontend/.env.example](frontend/.env.example) for the available settings. Backend values
belong in `backend/.env.local`; frontend values belong in `frontend/.env.local`.

| Variable | Purpose |
| --- | --- |
| `ORBIT_SILENCE_SECONDS` | Silence before Orbit responds; defaults to `1.5` seconds |
| `ORBIT_VISION_MODEL` | Gemini model used to interpret screenshots |
| `ORBIT_SEARCH_BACKEND` | Search engine selection; `auto` enables fallback engines |
| `ORBIT_DRY_RUN` | Set to `1` to simulate approved Google actions without executing them |
| `ORBIT_DATA_DIR` | Override the local data directory |
| `ORBIT_SECRETS_DIR` | Override the OAuth credentials directory |
| `ORBIT_CHROME_USER_DATA` | Override Chrome's profile directory |
| `ORBIT_ALLOWED_ORIGINS` | Comma-separated origins accepted by the API's CORS policy |
| `VITE_API_BASE_URL` | Frontend API base URL when hosted separately from the backend |
| `VITE_WAKE_WORD` | Set to `off` to disable wake-word listening in standby |

The wake word requires browser support for on-device speech recognition and microphone
permission. Pressing the orb remains available when local recognition is unsupported.

## Repository structure

| Path | Purpose |
| --- | --- |
| `backend/src/agent.py` | LiveKit voice agent and Gemini configuration |
| `backend/src/agent_tools/` | Voice tools for Google services, memory, browser, screen, and cards |
| `backend/src/api/` | FastAPI routes used by the frontend |
| `backend/src/integrations/` | Google OAuth, service clients, and web search |
| `backend/src/actions/` | Draft storage, approval gate, and action execution |
| `backend/src/drafting/` | Email triage and suggested replies |
| `backend/tests/` | Unit tests and model integration tests |
| `frontend/src/voice/` | LiveKit connection, microphone, and wake word |
| `frontend/src/stage/` | Card layout and window management |
| `frontend/src/cards/` | Cards for results, drafts, files, and reminders |

The frontend communicates with the backend over `/api` and with the voice agent over
LiveKit. See the [frontend README](frontend/README.md) for UI structure and build commands.

## Deployment and data

Orbit is intended for local development and personal use in its current form.

- **Authentication:** API routes are unauthenticated, including session token issuance,
  email, spreadsheet, and upload endpoints. Keep the API bound to localhost until
  authentication and authorization are added. CORS configuration does not provide access
  control.
- **Hosting:** microphone access requires HTTPS or localhost. If the frontend and API use
  different origins, configure `ORBIT_ALLOWED_ORIGINS` and `VITE_API_BASE_URL`.
- **Cloud processing:** voice conversations and screenshot analysis use Gemini through
  Google's APIs; voice transport uses LiveKit. Long-term memories are stored in Mem0,
  and the default email drafting models use Ollama's cloud.
- **Local storage:** reminders, action history, and reply drafts live under `backend/.data/`.
  Attachment copies are stored temporarily there. Session transcripts live in Redis and
  may also be written to disk if Redis persistence is enabled. Environment files,
  `.secrets/`, and `.data/` are ignored by Git.
- **Browser actions:** confirmation before submitting forms or changing accounts relies
  on model instructions; it does not use the Google action approval gate.
- **Current limits:** reminders notify only while the app is open. File previews are
  limited to 50 MB; email attachments to 25 MB total. Live-service and physical-device
  verification is incomplete; many integration tests use mocked services.
