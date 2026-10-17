import { useRef, useState, useSyncExternalStore } from 'react'
import type { DragEvent } from 'react'
import { apiSend, apiUpload } from './api.ts'
import type { FileView } from './files.ts'
import { useBackend } from './useBackend.ts'
import type { Loaded } from './useBackend.ts'

/** A file the backend keeps to go out with an email (backend/src/attachments.py). */
export type Attached = {
  id: string
  name: string
  /** What to call it: "PDF", "picture". */
  kind: string
  view: FileView
  /** As people say it: "340 KB". */
  size: string
  /** Where it came from, in words: "from your computer", "from Drive", "written by Orbit". */
  source: string
}

/** What Gmail lets one email carry in files. The backend holds to the same limit. */
const MAX_BYTES = 25_000_000

/** Called with the files the user picked or dropped, and with why any were left out. */
export type OnFiles = (files: File[], leftOut?: string) => void

/** Adds files one after another. Answers with which could not be added and why, or null. */
export async function addFiles(files: File[], add: (file: File) => Promise<unknown>): Promise<string | null> {
  const problems: string[] = []
  for (const file of files) {
    if (file.size > MAX_BYTES) {
      problems.push(`${file.name} is larger than the 25 MB an email can carry.`)
      continue
    }
    try {
      await add(file)
    } catch (e) {
      // A refusal comes with the backend's reason. A TypeError means it was never reached.
      const why = e instanceof Error && !(e instanceof TypeError) ? e.message : 'It could not be given to Orbit.'
      problems.push(`${file.name}: ${why}`)
    }
  }
  return problems.length > 0 ? problems.join(' ') : null
}

/** Puts a file on the reply waiting on an email's card. Answers with all the reply's files. */
export const attachToReply = (messageId: string, file: File) =>
  apiUpload<Attached[]>(`/api/mail/${messageId}/draft/attachments`, file)

/** Takes a file out of the tray, or off a reply. */
export const removeAttached = (id: string) => apiSend<{ removed: boolean }>('DELETE', `/api/attachments/${id}`)

// The tray: files waiting to be put on an email. Any part of the app can add to it, so its
// state is kept here and not in one component.
type Tray = { changes: number; adding: boolean; problem: string | null }
let tray: Tray = { changes: 0, adding: false, problem: null }
const watchers = new Set<() => void>()
const watch = (watcher: () => void) => {
  watchers.add(watcher)
  return () => void watchers.delete(watcher)
}
function set(next: Partial<Tray>) {
  tray = { ...tray, ...next }
  for (const watcher of watchers) watcher()
}

/** Puts files from the user's computer in the tray. Answers with the ones that got there. */
export async function addToTray(files: File[], leftOut?: string): Promise<Attached[]> {
  set({ adding: true, problem: null })
  const added: Attached[] = []
  const problem = await addFiles(files, async (file) => void added.push(await apiUpload<Attached>('/api/attachments', file)))
  set({ adding: false, changes: tray.changes + 1, problem: [leftOut, problem].filter(Boolean).join(' ') || null })
  return added
}

export async function removeFromTray(id: string) {
  // One that is already gone is as good as removed.
  await removeAttached(id).catch(() => undefined)
  set({ changes: tray.changes + 1 })
}

const TRAY_REFRESH_MS = 4000

/** Whether files are on their way to the tray, and what could not be added. */
export function useTrayState(): { changes: number; adding: boolean; problem: string | null } {
  return useSyncExternalStore(watch, () => tray)
}

/** The files in the tray, whether more are on their way, and what could not be added. */
export function useTray(): { files: Loaded<Attached[]>; adding: boolean; problem: string | null } {
  const { changes, adding, problem } = useTrayState()
  // Asked again every few seconds too, because Orbit puts the files it writes here.
  const files = useBackend<Attached[]>(`/api/attachments?seen=${changes}`, TRAY_REFRESH_MS)
  return { files, adding, problem }
}

// A picture dragged from the page itself also counts as files to the browser. Only what
// is dragged in from outside is a file being handed over.
let fromThisPage = false
document.addEventListener('dragstart', () => void (fromThisPage = true), true)
document.addEventListener('dragend', () => void (fromThisPage = false), true)

const carriesFiles = (e: DragEvent) => !fromThisPage && e.dataTransfer.types.includes('Files')

/** What was dropped, without the folders: a browser cannot hand over a folder as a file. */
function dropped(e: DragEvent): { files: File[]; leftOut?: string } {
  const files: File[] = []
  let folders = 0
  for (const item of e.dataTransfer.items) {
    if (item.kind !== 'file') continue
    const file = item.webkitGetAsEntry?.()?.isDirectory ? null : item.getAsFile()
    if (file) files.push(file)
    else folders += 1
  }
  return { files, leftOut: folders > 0 ? 'A folder cannot be added. Drop the files that are in it.' : undefined }
}

/**
 * Makes an element a place to drop files from the computer. `over` is true while files are
 * held over it. A drop on such a place inside another goes to the inner one only.
 */
export function useFileDrop(onFiles: OnFiles | undefined) {
  const [over, setOver] = useState(false)
  // A drag enters a child before it leaves the parent, so the two are counted.
  const depth = useRef(0)
  if (!onFiles) return { over: false, handlers: {} }
  return {
    over,
    handlers: {
      onDragEnter: (e: DragEvent) => {
        if (!carriesFiles(e)) return
        e.preventDefault()
        e.stopPropagation()
        depth.current += 1
        setOver(true)
      },
      // Without this the browser would open the file in place of the app.
      onDragOver: (e: DragEvent) => {
        if (!carriesFiles(e)) return
        e.preventDefault()
        e.stopPropagation()
        e.dataTransfer.dropEffect = 'copy'
      },
      onDragLeave: (e: DragEvent) => {
        if (!carriesFiles(e)) return
        e.stopPropagation()
        depth.current = Math.max(0, depth.current - 1)
        if (depth.current === 0) setOver(false)
      },
      onDrop: (e: DragEvent) => {
        if (!carriesFiles(e)) return
        e.preventDefault()
        e.stopPropagation()
        depth.current = 0
        setOver(false)
        const { files, leftOut } = dropped(e)
        if (files.length > 0 || leftOut) onFiles(files, leftOut)
      },
    },
  }
}
