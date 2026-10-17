import { AttachedFiles } from '../components/Attaching.tsx'
import { Formatted } from '../components/Formatted.tsx'
import { Icon } from '../components/Icon.tsx'
import type { IconName } from '../components/Icon.tsx'
import { Empty, Say } from '../components/ui.tsx'
import { useActions } from '../lib/actions.ts'
import type { ActionRecord } from '../lib/actions.ts'
import type { Attached } from '../lib/attachments.ts'
import { CardFrame } from '../stage/CardFrame.tsx'
import { useVoice } from '../voice/voice-context.ts'

const OUTCOME: Record<Exclude<ActionRecord['status'], 'awaiting_approval'>, string> = {
  running: 'In progress…',
  done: 'Done.',
  failed: 'It failed. Nothing was sent or changed.',
  cancelled: 'Cancelled. Nothing was sent or changed.',
  expired: 'It expired before you answered. Ask Orbit again if you still want it.',
}

function iconFor(kind: string): IconName {
  if (kind.startsWith('calendar')) return 'calendar'
  if (kind.startsWith('sheets')) return 'table'
  if (kind.startsWith('drive')) return 'file'
  return 'mail'
}

// The parts of a draft that are its writing. When Orbit wrote it with formatting, the
// draft also carries that writing as clean HTML, which is what will be sent or saved.
const WRITING = ['body', 'content']
// The parts that are not shown as a line of text: the title, the HTML, the table and the files.
const NOT_A_LINE = ['what', 'html', 'table', 'attachments']
// What a change to a sheet says in words, for Orbit to read out. Its table shows the same.
const IN_THE_TABLE = ['rows', 'changes', 'columns']

/** A change to a sheet as a table: the rows it adds or deletes, or each cell's old and new value. */
type DraftTable = { columns: string[]; rows: string[][] }

function tableOf(action: ActionRecord): DraftTable | null {
  const table = action.preview.table as Partial<DraftTable> | null | undefined
  if (!table || !Array.isArray(table.columns) || !Array.isArray(table.rows)) return null
  return {
    columns: table.columns.map(String),
    rows: table.rows.filter((row) => Array.isArray(row)).map((row) => row.map(String)),
  }
}

/** The draft's details, minus those shown some other way and anything left empty. */
function details(action: ActionRecord, table: DraftTable | null): [string, string][] {
  return Object.entries(action.preview)
    .filter(([key, value]) => !NOT_A_LINE.includes(key) && !(table && IN_THE_TABLE.includes(key)) && value !== null && value !== '')
    .map(([key, value]) => [key, Array.isArray(value) ? value.join(', ') : String(value)])
}

/** The files an email draft carries. */
function attachedTo(action: ActionRecord): Attached[] {
  const listed: unknown = action.preview.attachments
  if (!Array.isArray(listed)) return []
  return listed.filter(
    (file: unknown): file is Attached =>
      typeof file === 'object' && file !== null && typeof (file as Attached).id === 'string' && typeof (file as Attached).name === 'string',
  )
}

/** Something Orbit drafted in the user's name. It runs only after they say yes. */
export function ApprovalCard({ id, actionId }: { id: string; actionId: string }) {
  const voice = useVoice()
  const action = useActions(true)?.find((a) => a.id === actionId)

  if (!action) {
    return (
      <CardFrame id={id} title="Draft" icon="edit">
        <Empty icon="edit" title="Looking for the draft">It should appear in a moment.</Empty>
      </CardFrame>
    )
  }

  const awaiting = action.status === 'awaiting_approval'
  const done = action.status === 'done'
  const formatted = typeof action.preview.html === 'string' ? action.preview.html : null
  const table = tableOf(action)
  const files = attachedTo(action)
  // An email that was put in the user's Gmail drafts, to be finished there, and where it opens.
  const inGmail = action.gmail_draft?.startsWith('https://mail.google.com/') ? action.gmail_draft : null
  return (
    <CardFrame
      id={id}
      title={String(action.preview.what ?? action.kind)}
      icon={iconFor(action.kind)}
      tone={awaiting ? 'warm' : done ? 'ok' : undefined}
      tag={awaiting ? 'Awaiting go-ahead' : done ? 'Done' : inGmail ? 'In Gmail drafts' : action.status}
      awaiting={awaiting}
      pad
      foot={
        awaiting ? (
          <>
            <button type="button" className="btn btn--primary" disabled={!voice.connected} onClick={() => voice.send('Yes, go ahead.')}>
              <Icon name="check" />
              Go ahead <Say>yes, go ahead</Say>
            </button>
            <button type="button" className="btn btn--ghost" disabled={!voice.connected} onClick={() => voice.send('No, cancel that.')}>
              Cancel
            </button>
            {action.kind.startsWith('gmail.') && (
              <button
                type="button"
                className="btn btn--ghost win__aside"
                disabled={!voice.connected}
                onClick={() => voice.send('Put this draft in my Gmail drafts. I will finish it there.')}
                title="Puts this email in your Gmail drafts, with its files, for you to finish there. Nothing is sent."
              >
                Finish in Gmail
              </button>
            )}
          </>
        ) : inGmail ? (
          <a className="btn btn--sm" href={inGmail} target="_blank" rel="noopener noreferrer">
            <Icon name="open" />
            Open in Gmail
          </a>
        ) : undefined
      }
    >
      <dl className="kv">
        {details(action, table).map(([key, value]) => (
          <div style={{ display: 'contents' }} key={key}>
            <dt>{key}</dt>
            {formatted && WRITING.includes(key) ? (
              // Drawn as it will arrive, each link with where it leads beside it.
              <dd>
                <Formatted html={formatted} links="spelled" />
              </dd>
            ) : (
              <dd style={{ whiteSpace: 'pre-line', overflowWrap: 'anywhere' }}>{value}</dd>
            )}
          </div>
        ))}
        {files.length > 0 && (
          <div style={{ display: 'contents' }}>
            <dt>files</dt>
            {/* Kept by the backend only while the draft waits: after that they only name what it carried. */}
            <dd>
              <AttachedFiles files={files} openable={awaiting} />
            </dd>
          </div>
        )}
      </dl>
      {table && (
        <div className="sheet sheet--draft">
          <table>
            <thead>
              <tr>
                {table.columns.map((column, n) => (
                  <th scope="col" key={n}>
                    {column}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {table.rows.map((row, r) => (
                <tr key={r}>
                  {row.map((cell, n) => (
                    <td key={n}>
                      <div className="sheet__cell">{cell}</div>
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {action.status !== 'awaiting_approval' && (
        <p className={action.status === 'failed' ? 'is-error' : 'muted'} style={{ marginTop: 14 }}>
          {inGmail ? 'It is in your Gmail drafts for you to finish there. Nothing was sent.' : OUTCOME[action.status]}
        </p>
      )}
      {awaiting && !voice.connected && <p className="muted" style={{ marginTop: 14 }}>The conversation has ended, so this draft can no longer be approved.</p>}
    </CardFrame>
  )
}
