import { useBackend } from './useBackend.ts'
import type { Loaded } from './useBackend.ts'

/** One field of the job profile: its value, where it came from, and what other sources said. */
export type ProfileField = {
  key: string
  label: string
  value: string | string[] | null
  /** "you" when the user said it; otherwise the sources it came from, such as "résumé". */
  source: string
  others: { value: string; source: string }[]
  /** Asked for by most applications, so shown even when not known yet. */
  needed: boolean
  many: boolean
}

/** The user's job profile (backend/src/jobs/profile.py), as its card shows it. */
export type JobProfile = {
  groups: { title: string; fields: ProfileField[] }[]
  /** Every résumé of the user's, with the work each is for: each application sends the one that suits its job. */
  resumes: { name: string; source: string; role: string }[]
  answers: { question: string; answer: string }[]
  settings: { key: string; label: string; value: string }[]
}

/** The job profile. A new `shownAt` fetches it again, after it changed. */
export function useProfile(shownAt?: number): Loaded<JobProfile> {
  const at = shownAt ? `?at=${shownAt}` : ''
  return useBackend<JobProfile>(`/api/jobs/profile${at}`)
}
