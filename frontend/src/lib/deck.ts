import { useBackend } from './useBackend.ts'
import type { Loaded } from './useBackend.ts'

export type WaitingItem = {
  kind: 'email' | 'invitation'
  title: string
  detail: string
  /** ISO 8601: when the email arrived, or when the event starts. */
  at: string
  /** For emails: its id, Orbit's one-line read of it, and whether a reply is drafted. */
  message_id: string | null
  gist: string | null
  has_draft: boolean
}

export type DeckEvent = {
  title: string
  /** ISO 8601 with an offset, or a plain date for all-day events. */
  start: string
  end: string
  all_day: boolean
  place: string | null
}

export type DeckData = {
  connected: boolean
  waiting: WaitingItem[]
  /** Orbit is still reading some of the mail; gists and drafts are on their way. */
  reading: boolean
  /** The next seven days, soonest first. */
  events: DeckEvent[]
  /** One short reason per service that could not be read. */
  problems: string[]
}

// Every minute, and every few seconds while Orbit is still reading the mail.
const refresh = (deck: DeckData | null) => (deck?.reading ? 4_000 : 60_000)

/** What is waiting on the user and what is coming up. */
export function useDeck(): Loaded<DeckData> {
  return useBackend<DeckData>('/api/deck', refresh)
}
