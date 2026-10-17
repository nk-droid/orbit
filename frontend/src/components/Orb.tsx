import { useState } from 'react'
import type { CSSProperties } from 'react'
import type { OrbState } from '../voice/state.ts'

const BAR_COUNT = 48

type Props = {
  state: OrbState
  /** Any CSS length. Defaults to the size set in CSS for the orb's context. */
  size?: string
  /** Drops the fine detail (ticks, bars) that disappears at small sizes. */
  small?: boolean
  onClick?: () => void
  title?: string
}

function makeBars() {
  return Array.from({ length: BAR_COUNT }, (_, i) => ({
    angle: (i * 360) / BAR_COUNT,
    style: {
      animationDuration: `${(0.45 + Math.random() * 0.7).toFixed(2)}s`,
      animationDelay: `${(-Math.random() * 1.2).toFixed(2)}s`,
    },
  }))
}

/** Orbit's presence. All motion is CSS, driven by data-state. */
export function Orb({ state, size, small, onClick, title }: Props) {
  // Per-bar timing is random but fixed for the life of the orb, so bars don't re-sync on render.
  const [bars] = useState(makeBars)
  const className = `orb${small ? ' orb--sm' : ''}`
  const style = size ? ({ '--size': size } as CSSProperties) : undefined

  const body = (
    <>
      <span className="orb__halo" />
      <svg viewBox="0 0 200 200" aria-hidden="true">
        <circle className="orb__ticks" cx="100" cy="100" r="96" pathLength={360} strokeDasharray=".6 2.4" />
        <circle className="orb__ring" cx="100" cy="100" r="88" />
        <circle className="orb__arc orb__arc--a" cx="100" cy="100" r="80" pathLength={360} strokeDasharray="64 40 18 238" />
        <circle className="orb__arc orb__arc--b" cx="100" cy="100" r="71" pathLength={360} strokeDasharray="26 94 130 110" />
        <circle className="orb__dots" cx="100" cy="100" r="63" pathLength={360} strokeDasharray=".5 5.5" />
        <g className="orb__bars">
          {bars.map((bar) => (
            <g key={bar.angle} transform={`rotate(${bar.angle} 100 100)`}>
              <rect className="orb__bar" x="99.2" y="42" width="1.6" height="13" rx=".8" style={bar.style} />
            </g>
          ))}
        </g>
      </svg>
      <span className="orb__core" />
    </>
  )

  if (onClick) {
    return (
      <button type="button" className={className} data-state={state} style={style} onClick={onClick} title={title} aria-label={title}>
        {body}
      </button>
    )
  }
  return (
    <div className={className} data-state={state} style={style}>
      {body}
    </div>
  )
}
