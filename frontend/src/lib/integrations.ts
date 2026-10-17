import { useBackend } from './useBackend.ts'
import type { Loaded } from './useBackend.ts'

export type GoogleServiceName = 'gmail' | 'gcal' | 'gchat' | 'gdrive' | 'gsheets'

export type ServiceState = {
  label: string
  access: 'none' | 'read' | 'write'
  /** null when the backend did not check; otherwise whether the API answered. */
  working: boolean | null
  problem: string | null
}

export type GoogleState = {
  client_configured: boolean
  account: string | null
  problem: string | null
  services: Record<GoogleServiceName, ServiceState>
}

export type GoogleStatus = Loaded<GoogleState>

/** The Google connection as the backend sees it, with each API checked once. */
export function useGoogleStatus(): GoogleStatus {
  return useBackend<GoogleState>('/api/integrations/google?probe=true')
}
