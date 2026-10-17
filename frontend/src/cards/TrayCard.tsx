import { PickFiles } from '../components/Attaching.tsx'
import { Icon } from '../components/Icon.tsx'
import { Empty } from '../components/ui.tsx'
import { addToTray, removeFromTray, useTray } from '../lib/attachments.ts'
import { CardFrame } from '../stage/CardFrame.tsx'
import { useStage } from '../stage/stage-context.ts'

/**
 * The files waiting to be put on an email: the ones the user added from their computer,
 * and the ones Orbit wrote. Orbit attaches from here when asked to.
 */
export function TrayCard({ id }: { id: string }) {
  const stage = useStage()
  const { files, adding, problem } = useTray()
  const count = files?.length ?? 0

  return (
    <CardFrame
      id={id}
      title="Ready to attach"
      icon="clip"
      // Not "2 files": asked to close "the file", Orbit would then close this card too.
      tag={count > 0 ? `${count} waiting` : undefined}
      onFiles={addToTray}
      foot={
        <PickFiles className="btn btn--sm" onPick={addToTray} disabled={adding}>
          <Icon name="clip" />
          {adding ? 'Adding…' : 'Add files'}
        </PickFiles>
      }
    >
      {files === null && (
        <Empty icon="clip" title="Could not load">
          Orbit’s backend could not be reached.
        </Empty>
      )}
      {files?.length === 0 && (
        <Empty icon="clip" title="Nothing here yet">
          Orbit asks for a file when an email needs one from your computer. You can also drop one anywhere on Orbit, or press Add files.
        </Empty>
      )}
      {files?.map((file) => (
        <article className="row row--plain row--tools" key={file.id}>
          <div>
            <div className="row__title tray__name">{file.name}</div>
            <div className="row__line">
              {file.kind} · {file.size} · {file.source}
            </div>
          </div>
          <span className="row__tools">
            {file.view !== 'none' && (
              <button
                type="button"
                className="row__copy"
                onClick={() => stage.show({ id: `file:attached:${file.id}`, kind: 'file', from: 'attached', attached_id: file.id, title: file.name })}
                title="Show it here"
                aria-label={`Show ${file.name} here`}
              >
                <Icon name="eye" />
              </button>
            )}
            <button type="button" className="row__copy" onClick={() => removeFromTray(file.id)} title="Remove" aria-label={`Remove ${file.name}`}>
              <Icon name="x" />
            </button>
          </span>
        </article>
      ))}
      {problem && <p className="row__detail is-error tray__note">{problem}</p>}
      {count > 0 && (
        <p className="muted tray__note">
          Ask Orbit to attach one to an email. A file is removed once it has been sent, or after a day.
        </p>
      )}
    </CardFrame>
  )
}
