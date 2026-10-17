import { Fragment } from 'react'
import { Icon } from '../components/Icon.tsx'
import { Empty, Subhead } from '../components/ui.tsx'
import type { Reminder } from '../lib/reminders.ts'
import { shortWhen, useNow } from '../lib/time.ts'
import { useReminders } from '../reminders/reminders-context.ts'
import type { AlertState } from '../reminders/reminders-context.ts'
import { CardFrame } from '../stage/CardFrame.tsx'

const MINUTE = 60_000
// A reminder logged this recently is highlighted, so the one just spoken is easy to spot.
const FRESH_MS = 2 * MINUTE

const dayStart = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime()

/** "in 2 h 10 min", "5 min overdue". */
function fromNow(due: Date, now: Date): string {
  const minutes = Math.round(Math.abs(due.getTime() - now.getTime()) / MINUTE)
  const days = Math.floor(minutes / 1440)
  const hours = Math.floor(minutes / 60)
  const span =
    days >= 2 ? `${days} days` : hours >= 1 ? `${hours} h${minutes % 60 ? ` ${minutes % 60} min` : ''}` : `${minutes} min`
  if (minutes === 0) return 'now'
  return due < now ? `${span} overdue` : `in ${span}`
}

/** Open reminders under the heading for when they are due. */
function grouped(open: Reminder[], now: Date): { title: string; items: Reminder[] }[] {
  const today = dayStart(now)
  const day = 24 * 60 * MINUTE
  const due = (r: Reminder) => new Date(r.due_at).getTime()
  return [
    { title: 'Overdue', items: open.filter((r) => due(r) <= now.getTime()) },
    { title: 'Today', items: open.filter((r) => due(r) > now.getTime() && due(r) < today + day) },
    { title: 'Tomorrow', items: open.filter((r) => due(r) >= today + day && due(r) < today + 2 * day) },
    { title: 'Later', items: open.filter((r) => due(r) >= today + 2 * day) },
  ].filter((group) => group.items.length > 0)
}

const ALERTS: Record<AlertState, string> = {
  granted: 'Alerts are on while Orbit is open in a browser tab.',
  default: 'Orbit can notify you when a reminder comes due, while it is open.',
  denied: 'Notifications are blocked for this site in the browser’s settings.',
  unsupported: 'This browser cannot show notifications.',
}

/** The user's reminders by when they are due, with what was ticked off in the last day. */
export function RemindersCard({ id }: { id: string }) {
  const reminders = useReminders()
  const now = useNow(15_000)
  const all = reminders.list ?? []
  const open = all.filter((r) => r.status === 'open')
  const done = all.filter((r) => r.status === 'done')
  const overdue = open.filter((r) => new Date(r.due_at) <= now).length

  return (
    <CardFrame
      id={id}
      title="Reminders"
      icon="bell"
      tone={overdue > 0 ? 'warm' : undefined}
      tag={open.length > 0 ? (overdue > 0 ? `${overdue} due` : `${open.length} open`) : undefined}
      foot={
        <>
          <span className="muted" style={{ fontSize: 12 }}>{ALERTS[reminders.alerts]}</span>
          {reminders.alerts === 'default' && (
            <button type="button" className="btn btn--sm" style={{ marginLeft: 'auto' }} onClick={reminders.enableAlerts}>
              <Icon name="bell" />
              Turn on
            </button>
          )}
        </>
      }
    >
      {reminders.error && (
        <p className="row__detail is-error" style={{ padding: '10px 16px 0' }}>
          {reminders.error}
        </p>
      )}
      {all.length === 0 && (
        <Empty icon="bell" title={reminders.list === null ? 'Backend offline' : 'No reminders'}>
          {reminders.list === null
            ? 'Start the Orbit API to see your reminders.'
            : 'Tell Orbit “remind me tomorrow at five to call Asha”, and it is listed here.'}
        </Empty>
      )}
      {grouped(open, now).map((group) => (
        <Fragment key={group.title}>
          <Subhead>
            {group.title} · {group.items.length}
          </Subhead>
          {group.items.map((item) => {
            const due = new Date(item.due_at)
            const fresh = now.getTime() - new Date(item.created_at).getTime() < FRESH_MS
            return (
              <div className={`rem${due <= now ? ' is-late' : ''}${fresh ? ' is-new' : ''}`} key={item.id}>
                <button
                  type="button"
                  className="rem__box"
                  onClick={() => reminders.done(item.id)}
                  title="Mark done"
                  aria-label={`Mark “${item.title}” done`}
                />
                <div>
                  <b>{item.title}</b>
                  {item.note && <small>{item.note}</small>}
                </div>
                <div className="rem__when">
                  {shortWhen(due, now)}
                  <span>{fromNow(due, now)}</span>
                </div>
                <button
                  type="button"
                  className="win__btn"
                  onClick={() => reminders.cancel(item.id)}
                  title="Remove"
                  aria-label={`Remove “${item.title}”`}
                >
                  <Icon name="x" />
                </button>
              </div>
            )
          })}
        </Fragment>
      ))}
      {done.length > 0 && (
        <>
          <Subhead>Done in the last day · {done.length}</Subhead>
          {done.map((item) => (
            <div className="rem is-done" key={item.id}>
              <span className="rem__box" />
              <div>
                <b>{item.title}</b>
              </div>
              <div className="rem__when">{shortWhen(new Date(item.due_at), now)}</div>
              <button
                type="button"
                className="win__btn"
                onClick={() => reminders.reopen(item.id)}
                title="Undo"
                aria-label={`Undo “${item.title}”`}
              >
                <Icon name="undo" />
              </button>
            </div>
          ))}
        </>
      )}
    </CardFrame>
  )
}
