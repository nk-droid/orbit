import { useEffect, useState } from 'react'
import type { FileSource } from '../stage/stage-context.ts'
import { apiUrl } from './api.ts'
import { useBackend } from './useBackend.ts'
import type { Loaded } from './useBackend.ts'

/** How the app can show a file. Decided by the backend (backend/src/files.py). */
export type FileView = 'image' | 'pdf' | 'text' | 'markdown' | 'html' | 'none'

export type FileInfo = {
  name: string
  /** What to call it: "PDF", "Google Doc", "picture". */
  kind: string
  view: FileView
  /** The picture is Drive's preview of the file's first page, not the file itself. */
  preview: boolean
  /** The whole text, for a file shown as text. */
  text: string | null
  /** A Markdown or an HTML file as clean HTML to draw (components/Formatted.tsx). */
  html: string | null
  truncated: boolean
  /** Where the file opens in full, such as in Drive. */
  link: string | null
}

/** Where the backend answers for a file (backend/src/api/files.py). */
export function filePath(source: FileSource): string {
  const part = encodeURIComponent
  if (source.from === 'drive') return `/api/files/drive/${part(source.file_id)}`
  if (source.from === 'attached') return `/api/files/attached/${part(source.attached_id)}`
  return `/api/files/mail/${part(source.message_id)}/${part(source.part)}`
}

/** The file itself, at an address that opens it in a tab of its own. */
export const fileAddress = (source: FileSource) => apiUrl(`${filePath(source)}/content`)

/** What a file is and how to show it, with its text if it is text. */
export const useFile = (source: FileSource): Loaded<FileInfo> => useBackend<FileInfo>(filePath(source))

/**
 * A file once it has been fetched: where the page holds it, and what it turned out to be.
 * Now and then that is not what was asked for: a document too long for Google to make a
 * PDF of comes back as its text, which is then here as `text`. Or why it could not be fetched.
 */
export type Fetched = { url: string; type: string; text: string | null } | { problem: string }

/**
 * The bytes of a picture or a PDF, fetched whole and held by the page, so that a failure
 * is known and can be said instead of leaving a broken picture. `wanted` is false for a
 * file that is shown some other way. undefined while it loads.
 */
export function useFileBytes(source: FileSource, wanted: boolean): Fetched | undefined {
  const address = wanted ? fileAddress(source) : null
  const [fetched, setFetched] = useState<{ address: string; result: Fetched }>()

  useEffect(() => {
    if (address === null) return
    const controller = new AbortController()
    let url: string | undefined
    const load = async (): Promise<Fetched> => {
      const response = await fetch(address, { signal: controller.signal })
      if (!response.ok) {
        const detail: unknown = await response.json().then((data) => data.detail, () => null)
        throw new Error(typeof detail === 'string' ? detail : 'The file could not be fetched.')
      }
      const blob = await response.blob()
      url = URL.createObjectURL(blob)
      return { url, type: blob.type, text: blob.type.startsWith('text/') ? await blob.text() : null }
    }
    load().then(
      (result) => setFetched({ address, result }),
      (e: unknown) => {
        if (controller.signal.aborted) return
        setFetched({ address, result: { problem: e instanceof Error ? e.message : 'The file could not be fetched.' } })
      },
    )
    return () => {
      controller.abort()
      if (url) URL.revokeObjectURL(url)
    }
  }, [address])

  // What was fetched for another file is not this one's.
  return fetched?.address === address ? fetched.result : undefined
}
