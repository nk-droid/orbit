// Turns the backend's deck data into what the cards draw. Pure functions of the data and
// the current time, so a card stays correct as the clock moves.
import type { IconName } from '../components/Icon.tsx'
import type { DeckData, DeckEvent } from './deck.ts'
import { clockTime } from './time.ts'

const MINUTE = 60_000
const DATE_ONLY = /^(\d{4})-(\d{2})-(\d{2})$/

type Timed = { event: DeckEvent; start: Date; end: Date }

function parseWhen(value: string): Date {
  // A bare date is an all-day event: read it as local midnight, not UTC.
  const date = DATE_ONLY.exec(value)
  return date ? new Date(Number(date[1]), Number(date[2]) - 1, Number(date[3])) : new Date(value)
}

const startOfDay = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate())
const sameDay = (a: Date, b: Date) => startOfDay(a).getTime() === startOfDay(b).getTime()
const weekday = (d: Date) => d.toLocaleDateString('en-GB', { weekday: 'short' })
const dayLabel = (d: Date) => `${weekday(d)} ${String(d.getDate()).padStart(2, '0')}`

function nextDay(d: Date): Date {
  const next = startOfDay(d)
  next.setDate(next.getDate() + 1)
  return next
}

/** Events that have not ended yet, soonest first. */
function upcoming(deck: DeckData, now: Date): Timed[] {
  return deck.events
    .map((event) => ({ event, start: parseWhen(event.start), end: parseWhen(event.end) }))
    .filter(({ end }) => end.getTime() > now.getTime())
}

function length(start: Date, end: Date): string {
  const minutes = Math.round((end.getTime() - start.getTime()) / MINUTE)
  if (minutes < 60) return `${minutes} min`
  const rest = minutes % 60
  return rest ? `${Math.floor(minutes / 60)} h ${rest} min` : `${minutes / 60} h`
}

export type NeedsYouRow = {
  key: string
  icon: IconName
  title: string
  /** The email's or invitation's own words. */
  detail: string
  /** Orbit's one-line read of it, once it has one. */
  gist: string | null
  when: [string, string]
  /** For emails: which one to open, and whether Orbit has a reply drafted. */
  messageId: string | null
  hasDraft: boolean
}

export function needsYouRows(deck: DeckData, now: Date): NeedsYouRow[] {
  return deck.waiting.map((item, i) => {
    const at = parseWhen(item.at)
    return {
      key: `${item.kind}-${i}`,
      icon: item.kind === 'email' ? 'mail' : 'calendar',
      title: item.title,
      detail: item.detail,
      gist: item.gist,
      messageId: item.message_id,
      hasDraft: item.has_draft,
      when: [sameDay(at, now) ? 'Today' : dayLabel(at), DATE_ONLY.test(item.at) ? 'all day' : clockTime(at)],
    }
  })
}

export type TimelineItem = { key: string; time?: string; title: string; sub?: string; kind?: 'now' | 'day' }

/** The rest of today, then tomorrow. Empty when neither day has anything. */
export function timeline(deck: DeckData, now: Date): TimelineItem[] {
  const events = upcoming(deck, now)
  const tomorrow = nextDay(now)
  // Anything already under way counts as today, whenever it started.
  const todays = events.filter(({ start }) => start.getTime() < tomorrow.getTime())
  const tomorrows = events.filter(({ start }) => sameDay(start, tomorrow)).slice(0, 4)
  if (todays.length === 0 && tomorrows.length === 0) return []

  const row = ({ event, start, end }: Timed, i: number): TimelineItem => ({
    key: `${event.start}-${i}`,
    time: event.all_day ? 'all day' : clockTime(start),
    title: event.title,
    sub: [event.all_day ? null : length(start, end), event.place].filter(Boolean).join(' · ') || undefined,
  })

  return [
    { key: 'now', time: clockTime(now), title: 'Now', kind: 'now' },
    ...todays.map(row),
    ...(tomorrows.length > 0
      ? [{ key: 'tomorrow', title: `Tomorrow · ${dayLabel(tomorrow)}`, kind: 'day' as const }, ...tomorrows.map(row)]
      : []),
  ]
}
