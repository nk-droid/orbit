import { useEffect, useState } from 'react'

/** The current time, refreshed on an interval. */
export function useNow(intervalMs: number): Date {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), intervalMs)
    return () => clearInterval(id)
  }, [intervalMs])
  return now
}

const two = (n: number) => String(n).padStart(2, '0')

export const clockTime = (d: Date) => `${two(d.getHours())}:${two(d.getMinutes())}`

export const clockTimeSeconds = (d: Date) => `${clockTime(d)}:${two(d.getSeconds())}`

/** "Tue 06 Oct" */
export function shortDate(d: Date): string {
  const weekday = d.toLocaleDateString('en-GB', { weekday: 'short' })
  const month = d.toLocaleDateString('en-GB', { month: 'short' })
  return `${weekday} ${two(d.getDate())} ${month}`
}

/** "Tuesday 6 October" */
export const longDate = (d: Date) =>
  d.toLocaleDateString('en-GB', { weekday: 'long', day: 'numeric', month: 'long' })

/** "02:14" from a duration in milliseconds. */
export function duration(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000))
  return `${two(Math.floor(s / 60))}:${two(s % 60)}`
}

export function greeting(d: Date): string {
  const h = d.getHours()
  if (h < 12) return 'Good morning'
  if (h < 18) return 'Good afternoon'
  return 'Good evening'
}

/** "Today 17:58" or "Mon 24 Aug 17:58". */
export function shortWhen(d: Date, now: Date): string {
  const sameDay = d.toDateString() === now.toDateString()
  return `${sameDay ? 'Today' : shortDate(d)} ${clockTime(d)}`
}
