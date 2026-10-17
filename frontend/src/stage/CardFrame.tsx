import { useEffect } from 'react'
import type { PointerEvent as ReactPointerEvent, ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { Icon } from '../components/Icon.tsx'
import type { IconName } from '../components/Icon.tsx'
import { useFileDrop } from '../lib/attachments.ts'
import type { OnFiles } from '../lib/attachments.ts'
import { useStage, WIDTHS } from './stage-context.ts'

type Props = {
  id: string
  title: string
  icon: IconName
  /** warm = needs the user, ok = done. Leave out for a card that is only information. */
  tone?: 'warm' | 'ok'
  tag?: string
  /** The card is waiting on the user's go-ahead: the orb turns amber while it is in front. */
  awaiting?: boolean
  pad?: boolean
  /** The card takes all the height it may have, for something that fills it, such as a PDF. */
  tall?: boolean
  /** Given, the card takes files dropped on it, and they go nowhere else. */
  onFiles?: OnFiles
  foot?: ReactNode
  children: ReactNode
}

/**
 * The frame every card shares. On the stage it is a window that can be dragged by its
 * header, sent to the background or closed; in the background it is a chip on the shelf.
 * Where there is no room for windows, the layout places the one card that is shown.
 */
export function CardFrame({ id, title, icon, tone, tag, awaiting = false, pad, tall, onFiles, foot, children }: Props) {
  const stage = useStage()
  const drop = useFileDrop(onFiles)
  const { setAwaiting, setTitle } = stage
  const card = stage.cards.find((open) => open.spec.id === id)

  useEffect(() => {
    setAwaiting(id, awaiting)
    return () => setAwaiting(id, false)
  }, [id, awaiting, setAwaiting])

  useEffect(() => {
    setTitle(id, title, tag)
    return () => setTitle(id, null)
  }, [id, title, tag, setTitle])

  if (!card) return null

  if (card.back) {
    if (!stage.shelf) return null
    return createPortal(
      <span className="shelf__chip" data-tone={tone} onClick={() => stage.restore(id)} title="Bring back">
        <Icon name={icon} />
        <span className="shelf__name">
          {title}
          {tag && ` · ${tag}`}
        </span>
        <button
          type="button"
          className="shelf__x"
          onClick={(e) => {
            e.stopPropagation()
            stage.close(id)
          }}
          title="Close"
          aria-label={`Close ${title}`}
        >
          <Icon name="x" />
        </button>
      </span>,
      stage.shelf,
    )
  }

  const floating = stage.layout === 'windows'
  const drag = (e: ReactPointerEvent<HTMLElement>) => {
    if ((e.target as HTMLElement).closest('button')) return
    const header = e.currentTarget
    const from = { x: e.clientX, y: e.clientY, left: card.x, top: card.y }
    header.setPointerCapture(e.pointerId)
    const follow = (ev: PointerEvent) => stage.move(id, from.left + ev.clientX - from.x, from.top + ev.clientY - from.y)
    const drop = () => header.removeEventListener('pointermove', follow)
    header.addEventListener('pointermove', follow)
    header.addEventListener('pointerup', drop, { once: true })
    header.addEventListener('pointercancel', drop, { once: true })
  }

  return (
    <section
      className={`win panel${stage.frontId === id ? ' is-front' : ''}`}
      data-tone={tone}
      data-tall={tall ? '' : undefined}
      data-dropping={drop.over ? '' : undefined}
      {...drop.handlers}
      style={floating ? { left: card.x, top: card.y, zIndex: card.z, width: WIDTHS[card.spec.kind] } : undefined}
      onPointerDown={() => stage.frontId !== id && stage.front(id)}
    >
      <header className="panel__head win__head" onPointerDown={floating ? drag : undefined}>
        <span className={`src ${tone === 'warm' ? 'src--warm' : 'src--accent'}`}>
          <Icon name={icon} />
        </span>
        <span className="panel__title">{title}</span>
        {tag && <span className={`tag${tone ? ` tag--${tone}` : ''}`}>{tag}</span>}
        <span className="win__tools">
          <button
            type="button"
            className="win__btn"
            onClick={() => stage.toBack(id)}
            title="Send to background (Esc)"
            aria-label="Send to background"
          >
            <Icon name="minus" />
          </button>
          <button type="button" className="win__btn" onClick={() => stage.close(id)} title="Close" aria-label="Close">
            <Icon name="x" />
          </button>
        </span>
      </header>
      <div className={`panel__body${pad ? ' pad' : ''}`}>{children}</div>
      {foot && <footer className="panel__foot">{foot}</footer>}
    </section>
  )
}
