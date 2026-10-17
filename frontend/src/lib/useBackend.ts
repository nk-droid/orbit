import { useEffect, useState } from 'react'
import { apiUrl } from './api.ts'

/** undefined while loading, null when the backend could not be reached. */
export type Loaded<T> = T | null | undefined

/**
 * How long to wait before fetching again: a fixed time, or one chosen from what just
 * came back. 0 stops. A function must be defined once, outside the component.
 */
export type Refresh<T> = number | ((data: T | null) => number)

/** JSON from the backend, fetched on mount and again after `refresh` if given. A null path fetches nothing. */
export function useBackend<T>(path: string | null, refresh?: Refresh<T>): Loaded<T> {
  const [data, setData] = useState<Loaded<T>>(undefined)

  useEffect(() => {
    if (path === null) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    const load = async () => {
      const next = await fetch(apiUrl(path), { signal: controller.signal })
        .then((response) => (response.ok ? (response.json() as Promise<T>) : null))
        .catch(() => null)
      if (controller.signal.aborted) return
      setData(next)
      const wait = typeof refresh === 'function' ? refresh(next) : refresh
      if (wait) timer = setTimeout(load, wait)
    }
    void load()
    return () => {
      controller.abort()
      clearTimeout(timer)
    }
  }, [path, refresh])

  return data
}
