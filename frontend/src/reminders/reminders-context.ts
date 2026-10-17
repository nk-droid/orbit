import { createContext, useContext } from 'react'
import type { Reminder } from '../lib/reminders.ts'
import type { Loaded } from '../lib/useBackend.ts'

/** Whether this browser will show a notification when a reminder is due. */
export type AlertState = 'unsupported' | 'default' | 'granted' | 'denied'

/** The user's reminders, shared by the cards that show them and the due alerts. */
export type Reminders = {
  /** Open reminders soonest first, then the ones done in the last day. */
  list: Loaded<Reminder[]>
  /** Open reminders whose time has come. */
  dueCount: number
  /** Why the last change did not go through. */
  error: string | null
  done: (id: string) => void
  reopen: (id: string) => void
  cancel: (id: string) => void
  alerts: AlertState
  /** Asks the browser for permission. Must be called from a click. */
  enableAlerts: () => void
}

export const RemindersContext = createContext<Reminders | null>(null)

export function useReminders(): Reminders {
  const reminders = useContext(RemindersContext)
  if (!reminders) throw new Error('useReminders must be used inside <RemindersProvider>')
  return reminders
}
