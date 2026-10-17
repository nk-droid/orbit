// The only place the frontend knows where the backend lives.
// Empty in development: Vite proxies /api to the backend (see vite.config.ts).
const BASE: string = import.meta.env.VITE_API_BASE_URL ?? ''

export function apiUrl(path: string): string {
  return new URL(path, BASE || window.location.origin).toString()
}

// The custom header marks a request as coming from this app; the backend requires it.
const FROM_THE_APP = { 'X-Orbit-App': '1' }

/** The backend's answer, or its reason for refusing, thrown. */
async function answer<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const detail: unknown = await response.json().then((data) => data.detail, () => null)
    throw new Error(typeof detail === 'string' ? detail : `The request failed (${response.status}).`)
  }
  return response.json() as Promise<T>
}

/** Calls a backend route that changes something. Throws the backend's reason if it refuses. */
export async function apiSend<T>(method: 'POST' | 'PUT' | 'DELETE', path: string, body: unknown = {}): Promise<T> {
  return answer<T>(
    await fetch(apiUrl(path), {
      method,
      headers: { 'Content-Type': 'application/json', ...FROM_THE_APP },
      body: JSON.stringify(body),
    }),
  )
}

/** Gives the backend one file, as it is. Throws the backend's reason if it refuses. */
export async function apiUpload<T>(path: string, file: File): Promise<T> {
  const url = new URL(apiUrl(path))
  url.searchParams.set('name', file.name)
  return answer<T>(
    await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': file.type || 'application/octet-stream', ...FROM_THE_APP },
      body: file,
    }),
  )
}
