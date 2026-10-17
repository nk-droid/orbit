import { useEffect, useState } from 'react'
import { Icon } from '../components/Icon.tsx'
import { Empty } from '../components/ui.tsx'
import { CardFrame } from '../stage/CardFrame.tsx'
import type { CardSpec } from '../stage/stage-context.ts'
import { useStage } from '../stage/stage-context.ts'

/** Only a secure web address is ever made into a link, whatever a card was sent. */
function webLink(link: string | undefined): string | undefined {
  return link?.startsWith('https://') ? link : undefined
}

/** Copies a link, and shows for a moment that it did. */
function CopyLink({ link, label }: { link: string; label?: string }) {
  const [copied, setCopied] = useState(false)
  useEffect(() => {
    if (!copied) return
    const timer = setTimeout(() => setCopied(false), 1600)
    return () => clearTimeout(timer)
  }, [copied])
  const copy = () => {
    // Refused when the page is not focused or not served securely: then nothing was copied.
    navigator.clipboard.writeText(link).then(() => setCopied(true), () => setCopied(false))
  }
  return (
    <button
      type="button"
      className={label ? 'btn btn--sm btn--ghost' : 'row__copy'}
      onClick={copy}
      title="Copy link"
      aria-label="Copy link"
    >
      <Icon name={copied ? 'check' : 'copy'} />
      {label && (copied ? 'Copied' : label)}
    </button>
  )
}

/** Opens the whole card where it lives, such as a folder in Drive, or copies its link. */
export function OpenFoot({ link, label }: { link: string; label?: string }) {
  return (
    <>
      <a className="btn btn--sm" href={link} target="_blank" rel="noopener noreferrer">
        <Icon name="open" />
        {label || 'Open'}
      </a>
      <CopyLink link={link} label="Copy link" />
    </>
  )
}

/**
 * A list of what Orbit found: mail, events, files, chat messages or web results. Most are
 * to glance at, one line an entry. A `whole` list, such as a chat read back, shows every
 * entry in full to be read through.
 */
export function ListCard({ card }: { card: Extract<CardSpec, { kind: 'list' }> }) {
  const stage = useStage()
  const rows = card.rows ?? []
  const opens = webLink(card.link)
  return (
    <CardFrame
      id={card.id}
      title={card.title}
      icon={card.icon}
      tag={card.tag}
      foot={opens && <OpenFoot link={opens} label={card.link_label} />}
    >
      {rows.length === 0 && (
        <Empty icon={card.icon} title="Nothing found">Orbit looked and found nothing that matches.</Empty>
      )}
      {rows.map((row, i) => {
        const link = webLink(row.link)
        const fileId = row.file_id
        const sheetId = row.sheet_id
        // A file is shown as itself, and a Google Sheet as a table of its cells.
        const showIt = () =>
          sheetId
            ? stage.show({ id: `sheet:${sheetId}`, kind: 'sheet', spreadsheet_id: sheetId, title: row.title })
            : fileId && stage.show({ id: `file:${fileId}`, kind: 'file', from: 'drive', file_id: fileId, title: row.title })
        return (
          <article
            className={`row row--plain${card.whole ? ' row--whole' : ''}${link ? ' row--linked' : ''}${link || fileId || sheetId ? ' row--tools' : ''}`}
            key={i}
          >
            <div>
              <div className="row__title">
                {link ? (
                  <a href={link} target="_blank" rel="noopener noreferrer" title="Open in a new tab">
                    {row.title}
                  </a>
                ) : (
                  row.title
                )}
              </div>
              {row.line && <div className="row__line">{row.line}</div>}
              {row.detail && <p className="row__detail">{row.detail}</p>}
            </div>
            {row.meta && <span className="row__meta">{row.meta}</span>}
            {(link || fileId || sheetId) && (
              <span className="row__tools">
                {(fileId || sheetId) && (
                  <button
                    type="button"
                    className="row__copy"
                    onClick={showIt}
                    title="Show it here"
                    aria-label={`Show ${row.title} here`}
                  >
                    <Icon name="eye" />
                  </button>
                )}
                {link && <CopyLink link={link} />}
              </span>
            )}
          </article>
        )
      })}
      {card.more ? <p className="muted" style={{ padding: '10px 16px' }}>and {card.more} more</p> : null}
    </CardFrame>
  )
}

/**
 * What Orbit is doing in a browser, step by step. Each open browser has a card of its own,
 * told apart by the browser's name: "clean", or the profile it is on.
 */
export function BrowserCard({ card }: { card: Extract<CardSpec, { kind: 'browser' }> }) {
  const steps = card.steps ?? []
  const title = card.name ? `Browser · ${card.name}` : 'Browser'
  return (
    <CardFrame id={card.id} title={title} icon="globe" tag={card.shown ? 'Shown' : 'Hidden'}>
      <p className="label" style={{ padding: '14px 16px 4px' }}>
        {card.shown ? 'The browser is open on your screen.' : 'Working in a hidden browser.'}
      </p>
      {card.profile && (
        <p className="muted" style={{ padding: '0 16px 4px' }}>
          A copy of your {card.profile} profile, signed in as you.
        </p>
      )}
      {steps.length === 0 ? (
        <p className="muted" style={{ padding: '6px 16px 16px' }}>Started. Nothing opened yet.</p>
      ) : (
        <div className="steps">
          {steps.map((step, i) => (
            <div className="step is-done" key={i}>
              <span className="step__mark">
                <Icon name="check" />
              </span>
              <div>
                <b>{step}</b>
              </div>
            </div>
          ))}
        </div>
      )}
    </CardFrame>
  )
}
