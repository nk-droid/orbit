import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { apiUrl } from '../lib/api.ts'
import { changeReminder } from '../lib/reminders.ts'
import type { Reminder, ReminderChange } from '../lib/reminders.ts'
import { useNow } from '../lib/time.ts'
import type { Loaded } from '../lib/useBackend.ts'
import { RemindersContext } from './reminders-context.ts'
import type { AlertState, Reminders } from './reminders-context.ts'

const REFRESH_MS = 20_000
// A reminder that came due while Orbit was closed is shown as overdue, not announced late.
const ALERT_WINDOW_MS = 10 * 60_000
const ALERTED_KEY = 'orbit.reminders.alerted'

const alertState = (): AlertState => ('Notification' in window ? Notification.permission : 'unsupported')

/** Which reminders this browser has already announced, so a refresh does not repeat them. */
function readAlerted(): Set<string> {
  try {
    return new Set(JSON.parse(localStorage.getItem(ALERTED_KEY) ?? '[]') as string[])
  } catch {
    return new Set()
  }
}

function announce(reminder: Reminder) {
  const options = { body: reminder.title, tag: reminder.id }
  try {
    new Notification('Orbit reminder', options)
  } catch {
    // Some browsers only allow notifications through the service worker.
    void navigator.serviceWorker?.getRegistration().then((worker) => worker?.showNotification('Orbit reminder', options))
  }
}

/** Keeps the reminders in step with the backend and announces each one as it comes due. */
export function RemindersProvider({ children }: { children: ReactNode }) {
  const [list, setList] = useState<Loaded<Reminder[]>>(undefined)
  const [version, setVersion] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [alerts, setAlerts] = useState<AlertState>(alertState)
  const alerted = useRef<Set<string> | null>(null)
  const now = useNow(15_000)

  useEffect(() => {
    const controller = new AbortController()
    const load = () => {
      fetch(apiUrl('/api/reminders'), { signal: controller.signal })
        .then((response) => (response.ok ? (response.json() as Promise<Reminder[]>) : null))
        .then(setList)
        .catch(() => {
          if (!controller.signal.aborted) setList(null)
        })
    }
    load()
    const timer = setInterval(load, REFRESH_MS)
    return () => {
      controller.abort()
      clearInterval(timer)
    }
  }, [version])

  useEffect(() => {
    if (!list || alerts !== 'granted') return
    alerted.current ??= readAlerted()
    const seen = alerted.current
    for (const reminder of list) {
      const late = now.getTime() - new Date(reminder.due_at).getTime()
      // Keyed by time as well, so a reminder moved to a new time is announced again.
      const key = `${reminder.id}@${reminder.due_at}`
      if (reminder.status !== 'open' || late < 0 || late > ALERT_WINDOW_MS || seen.has(key)) continue
      seen.add(key)
      announce(reminder)
    }
    try {
      localStorage.setItem(ALERTED_KEY, JSON.stringify([...seen].slice(-200)))
    } catch {
      // Without storage the worst case is a repeated alert after a reload.
    }
  }, [list, now, alerts])

  const change = useCallback((id: string, to: ReminderChange) => {
    setError(null)
    changeReminder(id, to)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : 'That did not go through.'))
      .finally(() => setVersion((v) => v + 1))
  }, [])

  const enableAlerts = useCallback(() => {
    if (!('Notification' in window)) return
    void Notification.requestPermission().then(setAlerts)
  }, [])

  const value = useMemo<Reminders>(() => {
    const dueCount = (list ?? []).filter(
      (reminder) => reminder.status === 'open' && new Date(reminder.due_at) <= now,
    ).length
    return {
      list,
      dueCount,
      error,
      done: (id) => change(id, 'done'),
      reopen: (id) => change(id, 'reopen'),
      cancel: (id) => change(id, 'cancel'),
      alerts,
      enableAlerts,
    }
  }, [list, now, error, alerts, change, enableAlerts])

  return <RemindersContext.Provider value={value}>{children}</RemindersContext.Provider>
}
