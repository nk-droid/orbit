import { Empty, Src } from '../components/ui.tsx'
import { useDeck } from '../lib/deck.ts'
import type { DeckData } from '../lib/deck.ts'
import { needsYouRows } from '../lib/deckView.ts'
import { clockTime, useNow } from '../lib/time.ts'
import { useReminders } from '../reminders/reminders-context.ts'
import { CardFrame } from '../stage/CardFrame.tsx'
import { useStage } from '../stage/stage-context.ts'
import { useVoice } from '../voice/voice-context.ts'

const NOTHING: DeckData = { connected: false, waiting: [], events: [], problems: [], reading: false }

/** What is waiting on the user: mail, invitations not answered, and reminders that have come due. */
export function NeedsCard({ id }: { id: string }) {
  const stage = useStage()
  const voice = useVoice()
  const reminders = useReminders()
  const now = useNow(60_000)
  const loaded = useDeck()
  const deck = loaded ?? NOTHING
  const rows = needsYouRows(deck, now)
  const due = (reminders.list ?? []).filter((r) => r.status === 'open' && new Date(r.due_at) <= now)
  const count = rows.length + due.length

  const ask = (text: string) => (voice.connected ? voice.send(text) : voice.start())

  return (
    <CardFrame id={id} title="Needs you" icon="orbit" tone={count > 0 ? 'warm' : undefined} tag={count > 0 ? String(count) : undefined}>
      {deck.problems.map((problem) => (
        <p className="row__detail is-error" style={{ padding: '10px 16px 0' }} key={problem}>
          {problem}
        </p>
      ))}
      {count === 0 && (
        <Empty icon="check" title={loaded === undefined ? 'Checking' : 'Nothing waiting'}>
          {loaded === null
            ? 'Orbit can’t reach its backend.'
            : !deck.connected && loaded !== undefined
              ? 'Orbit isn’t connected to your mail or calendar yet.'
              : 'Mail that wants an answer, invitations you haven’t answered and reminders that are due show up here.'}
        </Empty>
      )}
      {due.map((reminder) => (
        <article className="row" key={reminder.id}>
          <Src icon="bell" tone="warm" />
          <div>
            <div className="row__title">{reminder.title}</div>
            {reminder.note && <p className="row__detail">{reminder.note}</p>}
            <div className="row__actions">
              <button type="button" className="btn btn--sm" onClick={() => reminders.done(reminder.id)}>
                Done
              </button>
            </div>
          </div>
          <span className="row__meta">
            Due
            <br />
            {clockTime(new Date(reminder.due_at))}
          </span>
        </article>
      ))}
      {rows.map((item) => (
        <article className="row" key={item.key}>
          <Src icon={item.icon} tone="warm" />
          <div>
            <div className="row__title">{item.title}</div>
            {item.gist ? <p className="gist">{item.gist}</p> : <p className="row__detail">{item.detail}</p>}
            <div className="row__actions">
              {item.messageId ? (
                <button
                  type="button"
                  className={`btn btn--sm${item.hasDraft ? ' btn--primary' : ''}`}
                  onClick={() => stage.show({ id: `mail:${item.messageId}`, kind: 'mail', message_id: item.messageId! })}
                >
                  {item.hasDraft ? 'Review draft' : 'Open'}
                </button>
              ) : (
                <button type="button" className="btn btn--sm" onClick={() => ask(`Tell me about the invitation “${item.title}”.`)}>
                  Ask Orbit
                </button>
              )}
            </div>
          </div>
          <span className="row__meta">
            {item.when[0]}
            <br />
            {item.when[1]}
          </span>
        </article>
      ))}
    </CardFrame>
  )
}
