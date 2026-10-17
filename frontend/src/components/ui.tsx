import type { ReactNode } from 'react'
import type { Tone } from '../lib/types.ts'
import { Icon } from './Icon.tsx'
import type { IconName } from './Icon.tsx'

export function Tag({ tone, children }: { tone?: Tone; children: ReactNode }) {
  return <span className={`tag${tone ? ` tag--${tone}` : ''}`}>{children}</span>
}

type PanelProps = {
  title: string
  /** Right-aligned detail in the header. */
  meta?: ReactNode
  /** Sits next to the title, usually a Tag. */
  tag?: ReactNode
  foot?: ReactNode
  /** Pads the body; leave off when the body is rows that carry their own padding. */
  pad?: boolean
  className?: string
  children: ReactNode
}

export function Panel({ title, meta, tag, foot, pad, className, children }: PanelProps) {
  return (
    <section className={`panel${className ? ` ${className}` : ''}`}>
      <header className="panel__head">
        <span className="panel__title">{title}</span>
        {tag}
        {meta && <span className="panel__meta">{meta}</span>}
      </header>
      <div className={`panel__body${pad ? ' pad' : ''}`}>{children}</div>
      {foot && <footer className="panel__foot">{foot}</footer>}
    </section>
  )
}

export function Subhead({ icon, children }: { icon?: IconName; children: ReactNode }) {
  return (
    <h3 className="subhead">
      {icon && <Icon name={icon} />}
      {children}
    </h3>
  )
}

/** Initials avatar for a person. */
export function Av({ you, children }: { you?: boolean; children: ReactNode }) {
  return <span className={`av${you ? ' av--you' : ''}`}>{children}</span>
}

/** Square glyph for where something came from (mail, Slack, calendar). */
export function Src({ icon, tone }: { icon: IconName; tone?: Tone }) {
  return (
    <span className={`src${tone ? ` src--${tone}` : ''}`}>
      <Icon name={icon} />
    </span>
  )
}

/** A block of Orbit's own words: summaries, reasoning, suggestions. */
export function Note({ icon = 'orbit', title, children }: { icon?: IconName; title: string; children: ReactNode }) {
  return (
    <div className="note">
      <div className="note__head">
        <Icon name={icon} />
        {title}
      </div>
      {children}
    </div>
  )
}

/** The spoken phrase that triggers the button it sits in. */
export function Say({ children }: { children: string }) {
  return <span className="say">say “{children}”</span>
}

export function Check({ on, children }: { on?: boolean; children: ReactNode }) {
  return <div className={`check${on ? ' is-on' : ''}`}>{children}</div>
}

export function Empty({ icon, title, children }: { icon: IconName; title: string; children: ReactNode }) {
  return (
    <div className="empty">
      <Icon name={icon} />
      <b>{title}</b>
      <p>{children}</p>
    </div>
  )
}
