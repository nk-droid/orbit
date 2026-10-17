import { useRef } from 'react'
import type { ReactNode } from 'react'
import type { Attached } from '../lib/attachments.ts'
import { useStage } from '../stage/stage-context.ts'
import { Icon } from './Icon.tsx'

type PickProps = {
  onPick: (files: File[]) => void
  className?: string
  /** What the button does, for a button that is only an icon. */
  label?: string
  disabled?: boolean
  children: ReactNode
}

/** A button that opens the computer's file picker and hands over what was picked. */
export function PickFiles({ onPick, className, label, disabled, children }: PickProps) {
  const input = useRef<HTMLInputElement>(null)
  return (
    <>
      <input
        ref={input}
        type="file"
        multiple
        hidden
        tabIndex={-1}
        onChange={(e) => {
          const files = [...(e.target.files ?? [])]
          // Emptied, so that picking the same file a second time is still a change.
          e.target.value = ''
          if (files.length > 0) onPick(files)
        }}
      />
      <button type="button" className={className} title={label} aria-label={label} disabled={disabled} onClick={() => input.current?.click()}>
        {children}
      </button>
    </>
  )
}

type AttachedProps = {
  files: Attached[]
  /** False once the backend no longer keeps the files, as after the email has gone. */
  openable?: boolean
  onRemove?: (file: Attached) => void
}

/** The files on a draft. One the app can show opens in a card of its own. */
export function AttachedFiles({ files, openable = true, onRemove }: AttachedProps) {
  const stage = useStage()
  if (files.length === 0) return null
  return (
    <div className="attached">
      {files.map((file) => {
        const named = (
          <>
            <Icon name={file.view === 'image' ? 'image' : 'file'} />
            <span className="attach__name">{file.name}</span>
            <small>{file.size}</small>
          </>
        )
        return (
          <span className="attach" key={file.id} title={`${file.kind}, ${file.source}`}>
            {openable && file.view !== 'none' ? (
              <button
                type="button"
                className="attach__open"
                onClick={() => stage.show({ id: `file:attached:${file.id}`, kind: 'file', from: 'attached', attached_id: file.id, title: file.name })}
                title="Show it here"
              >
                {named}
              </button>
            ) : (
              named
            )}
            {onRemove && (
              <button type="button" className="attach__x" onClick={() => onRemove(file)} title="Remove" aria-label={`Remove ${file.name}`}>
                <Icon name="x" />
              </button>
            )}
          </span>
        )
      })}
    </div>
  )
}
