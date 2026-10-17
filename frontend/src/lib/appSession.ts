import { useEffect } from 'react'
import { apiUrl } from './api.ts'

const KEPT_AS = 'orbit.session'
const ID = /^[0-9a-f-]{16,64}$/
// How often the open app tells the backend so (backend/src/session_memory.py keeps what
// was said for 15 minutes after it last heard).
const ALIVE_EVERY_MS = 5 * 60_000

function newId(): string {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID()
  // randomUUID needs a secure page; random bytes do not.
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, '0')).join('')
}

/**
 * This tab's session of Orbit: from opening the app until the tab is closed. What is said
 * in it is remembered across conversations, standby included. Kept in sessionStorage,
 * which a reload of the tab keeps and closing it clears.
 */
export const APP_SESSION: string = (() => {
  try {
    const kept = sessionStorage.getItem(KEPT_AS)
    if (kept && ID.test(kept)) return kept
    const made = newId()
    sessionStorage.setItem(KEPT_AS, made)
    return made
  } catch {
    // No storage, as in some private windows: a session for as long as this page lasts.
    return newId()
  }
})()

/** What the agent is dispatched with, so it knows which session it is in. */
export const AGENT_METADATA = JSON.stringify({ session: APP_SESSION })

function tell(what: 'alive' | 'closing') {
  // keepalive lets the request go out while the tab closes. The header marks it as the app's.
  void fetch(apiUrl(`/api/session/${APP_SESSION}/${what}`), {
    method: 'POST',
    keepalive: true,
    headers: { 'X-Orbit-App': '1' },
  }).catch(() => undefined)
}

/**
 * Keeps this tab's session remembered while the app is open. When the tab closes, what was
 * said is let go a minute later; a reload says it is open again before then.
 */
export function useAppSessionAlive() {
  useEffect(() => {
    tell('alive')
    const timer = setInterval(() => tell('alive'), ALIVE_EVERY_MS)
    const closing = () => tell('closing')
    // A page restored from the browser's back-forward cache is open again.
    const back = (event: PageTransitionEvent) => event.persisted && tell('alive')
    window.addEventListener('pagehide', closing)
    window.addEventListener('pageshow', back)
    return () => {
      clearInterval(timer)
      window.removeEventListener('pagehide', closing)
      window.removeEventListener('pageshow', back)
    }
  }, [])
}
