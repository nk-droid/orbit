# Orbit frontend

React + TypeScript PWA, built with Vite. See the [root README](../README.md) for how to run it
with the backend.

```sh
npm run dev      # dev server on :5173, proxies /api to the backend on :8000
npm run build    # type-check, then build to dist/ with the service worker
npm run preview  # serve the built PWA on :4173
npm run lint
```

## Where things live

| Path | Purpose |
|---|---|
| `src/lib/api.ts` | The only place that knows the backend's address. The rest of `src/lib/` fetches what each card shows. |
| `src/voice/` | The only place that touches LiveKit. `useVoice()` gives the UI the session state, transcript and controls. The wake word is here too. |
| `src/home/` | The home: Orbit in the middle, and the command bar (`Dock.tsx`). |
| `src/stage/` | The cards around Orbit: opening, moving and the background shelf, and the layout for each window size. |
| `src/cards/` | One component per kind of card. |
| `src/reminders/` | The reminders, and the alerts when one comes due. |
| `src/components/` | Shared presentational pieces: `Orb`, `Icon`, `Panel`, `Tag` and so on. |
| `src/styles/orbit.css` | The design system. `app.css` holds additions for the live app, and `stage.css` the cards. |

Colour carries state, not source: cyan is Orbit, amber needs you, red was held back, green is done.
