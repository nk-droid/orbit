import { useBackend } from './useBackend.ts'
import type { Loaded } from './useBackend.ts'

export type ActionStatus = 'awaiting_approval' | 'running' | 'done' | 'failed' | 'cancelled' | 'expired'

/** Something Orbit drafted or did in the user's name. */
export type ActionRecord = {
  id: string
  /** For example "gmail.send_reply" or "calendar.create_event". */
  kind: string
  /** The draft as the user was told it. "what" names the action; the rest are its details. */
  preview: Record<string, unknown>
  status: ActionStatus
  error: string | null
  /** Where the email opens in Gmail, if it was put in the drafts there instead of being sent. */
  gmail_draft: string | null
  /** Seconds since the epoch. */
  created_at: number
  updated_at: number
}

const POLL_MS = 2000

/** Recent actions, newest first. Polled while `live`, so a draft shows up as it is made. */
export function useActions(live: boolean): Loaded<ActionRecord[]> {
  return useBackend<ActionRecord[]>('/api/actions?limit=10', live ? POLL_MS : undefined)
}
