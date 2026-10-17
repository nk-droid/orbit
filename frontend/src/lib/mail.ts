import { apiSend } from './api.ts'
import type { Attached } from './attachments.ts'
import type { FileView } from './files.ts'
import { useBackend } from './useBackend.ts'
import type { Loaded } from './useBackend.ts'

/**
 * A reply Orbit suggested. Nothing is sent until the user sends it. `in_gmail` means it was
 * put in the user's Gmail drafts to be finished there, and `link` is where it opens. The
 * files it carries and the link come with one email, not with a list of them.
 */
export type Draft = {
  body: string
  status: 'suggested' | 'sending' | 'sent' | 'in_gmail' | 'discarded' | 'none'
  attachments: Attached[]
  link: string | null
}

export type MailItem = {
  id: string
  sender: string
  address: string
  subject: string
  /** ISO 8601. */
  received_at: string
  snippet: string
  /** Orbit's one-line read of the email. null while Orbit is still reading it. */
  gist: string | null
  needs_reply: boolean | null
  draft: Draft | null
}

/** A file that came with an email: its name, the part of the email it is in, and how it can be shown. */
export type Attachment = { name: string; part: string; view: FileView }

export type MailDetail = MailItem & { to: string; body: string; body_truncated: boolean; attachments: Attachment[] }

export type MailList = { connected: boolean; reading: boolean; items: MailItem[]; problem: string | null }

// Every minute, and every few seconds while Orbit is still reading, so drafts appear as they are written.
const refreshList = (list: MailList | null) => (list?.reading ? 4_000 : 60_000)
const refreshMail = (mail: MailDetail | null) => (mail && mail.needs_reply === null ? 4_000 : 0)

/** The mail waiting on the user, each with Orbit's read of it. */
export const useWaitingMail = (): Loaded<MailList> => useBackend<MailList>('/api/mail/waiting', refreshList)

export const useMail = (id: string | null): Loaded<MailDetail> =>
  useBackend<MailDetail>(id ? `/api/mail/${id}` : null, refreshMail)

export const saveDraft = (id: string, body: string) => apiSend<Draft>('PUT', `/api/mail/${id}/draft`, { body })

/**
 * Sends the reply. The backend refuses unless `body` is exactly the saved draft and
 * `attachments` are exactly the files on it: the go-ahead is for what was on screen.
 */
export const sendDraft = (id: string, body: string, attachments: string[]) =>
  apiSend<Draft>('POST', `/api/mail/${id}/draft/send`, { body, attachments })

/** Puts the reply in the user's Gmail drafts, files and all, to be finished there. Nothing is sent. */
export const draftToGmail = (id: string, body: string, attachments: string[]) =>
  apiSend<Draft>('POST', `/api/mail/${id}/draft/to-gmail`, { body, attachments })

export const discardDraft = (id: string) => apiSend<Draft>('POST', `/api/mail/${id}/draft/discard`)
