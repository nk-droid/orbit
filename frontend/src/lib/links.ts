import type { IconName } from '../components/Icon.tsx'
import type { GoogleServiceName, GoogleStatus, ServiceState } from './integrations.ts'

export type LinkRow = {
  icon: IconName
  name: string
  status: string
  /** ok = connected and answering, bad = connected but failing, off = not connected. */
  tone: 'ok' | 'bad' | 'off'
  /** The full reason, shown on hover. */
  detail?: string
}

const GOOGLE_ROWS: { service: GoogleServiceName; icon: IconName; name: string }[] = [
  { service: 'gmail', icon: 'mail', name: 'Gmail' },
  { service: 'gcal', icon: 'calendar', name: 'Calendar' },
  { service: 'gchat', icon: 'chat', name: 'Chat' },
  { service: 'gdrive', icon: 'file', name: 'Drive' },
  { service: 'gsheets', icon: 'table', name: 'Sheets' },
]

function serviceRow(state: ServiceState | undefined): Pick<LinkRow, 'status' | 'tone' | 'detail'> {
  // A backend from before a service was added does not report it.
  if (!state || state.access === 'none') return { status: 'not connected', tone: 'off' }
  if (state.working === false) {
    // Google's wording for an API that was never switched on in the Cloud project.
    const notEnabled = /has not been used|is disabled/.test(state.problem ?? '')
    return { status: notEnabled ? 'API not enabled' : 'not working', tone: 'bad', detail: state.problem ?? undefined }
  }
  return { status: state.access === 'write' ? 'connected' : 'connected · read only', tone: 'ok' }
}

/** What Orbit can reach. Reminders are Orbit's own; web search and the browser need no account. */
export function linkRows(google: GoogleStatus): LinkRow[] {
  const googleRows = GOOGLE_ROWS.map(({ service, icon, name }): LinkRow => {
    if (google === undefined) return { icon, name, status: 'checking…', tone: 'off' }
    if (google === null) return { icon, name, status: 'backend offline', tone: 'bad' }
    return { icon, name, ...serviceRow(google.services[service]) }
  })
  return [
    ...googleRows,
    { icon: 'bell', name: 'Reminders', status: 'built in', tone: 'ok' },
    { icon: 'globe', name: 'Web search', status: 'built in', tone: 'ok' },
  ]
}
