import { apiSend } from './api.ts'

/** A note to self with a time, kept by Orbit on this machine. */
export type Reminder = {
  id: string
  title: string
  note: string | null
  /** ISO 8601. */
  due_at: string
  status: 'open' | 'done' | 'cancelled'
  /** ISO 8601. */
  created_at: string
}

export type ReminderChange = 'done' | 'reopen' | 'cancel'

export const changeReminder = (id: string, change: ReminderChange) =>
  apiSend<Reminder>('POST', `/api/reminders/${id}/${change}`)
