import { useState } from 'react'
import type { ChartView } from '../lib/charts.ts'
import { Cartesian } from './Cartesian.tsx'
import { formatLabel, formatNumber } from './format.ts'
import { colourOf } from './palette.ts'
import { KEY_BESIDE, Pie } from './Pie.tsx'
import { useWidth } from './useSize.ts'

/** What a tooltip shows: a heading, and a line for each value, with its colour. */
export type Tip = { x: number; y: number; title: string; lines: { name: string; value: string; colour: string }[] }
export type OnTip = (tip: Tip | null) => void

/** The chart's names, in the order its colours are given. */
function seriesNames(view: ChartView): string[] {
  if (view.kind === 'scatter') return view.series_names ?? []
  if (view.kind === 'histogram') return []
  return (view.series ?? []).map((series) => series.name)
}

/** What a chart shows, in words, for a screen reader: the same gist Orbit says. */
function described(view: ChartView): string {
  const parts = [`${view.title}.`]
  if ((view.kind === 'pie' || view.kind === 'donut' || view.kind === 'bar') && view.series?.length === 1) {
    const values = view.series[0].values
    const top = values.reduce<number>((best, value, n) => ((value ?? -Infinity) > (values[best] ?? -Infinity) ? n : best), 0)
    if (view.categories?.[top] !== undefined && values[top] !== null) {
      parts.push(`Largest: ${view.categories[top]}, ${formatNumber(values[top] as number, view.y)}.`)
    }
  }
  return parts.join(' ')
}

/** How tall a chart is drawn: less on a phone, and more for many bars from the left. */
function heightOf(view: ChartView, width: number): number {
  const base = width && width < 420 ? 240 : 300
  if (view.kind !== 'bar' || view.orientation !== 'horizontal') return base
  const rows = view.categories?.length ?? 0
  const across = view.stacking === 'none' ? (view.series?.length ?? 1) : 1
  return Math.max(base, rows * Math.max(18, across * 10 + 8) + 50)
}

/**
 * A chart drawn from what the backend worked out, as wide as its card. Series can be hidden
 * from the legend, and a value is shown on hover or tap.
 */
export function Chart({ view, height }: { view: ChartView; height?: number }) {
  const [box, width] = useWidth<HTMLDivElement>()
  const [hidden, setHidden] = useState<ReadonlySet<string>>(new Set())
  const [tip, setTip] = useState<Tip | null>(null)
  const drawnHeight = height ?? heightOf(view, width)
  const names = seriesNames(view)
  const round = view.kind === 'pie' || view.kind === 'donut'
  const toggle = (name: string) =>
    setHidden((was) => {
      const next = new Set(was)
      if (next.has(name)) next.delete(name)
      else if (next.size < names.length - 1) next.add(name)
      return next
    })

  return (
    <div className="chart" ref={box} onMouseLeave={() => setTip(null)}>
      {width > 0 && (
        <svg className="chart__svg" role="img" width={width} height={drawnHeight}>
          <title>{view.title}</title>
          <desc>{described(view)}</desc>
          {round ? (
            <Pie view={view} width={width} height={drawnHeight} onTip={setTip} />
          ) : (
            <Cartesian view={view} width={width} height={drawnHeight} hidden={hidden} onTip={setTip} />
          )}
        </svg>
      )}
      {tip && (
        <div
          className="chart__tip"
          style={{
            left: Math.min(Math.max(8, tip.x), Math.max(8, width - 8)),
            top: tip.y,
            transform: `translate(${tip.x > width * 0.6 ? 'calc(-100% - 10px)' : '10px'}, -50%)`,
          }}
          role="status"
        >
          <b>{tip.title}</b>
          {tip.lines.map((line) => (
            <span key={line.name}>
              <i style={{ background: line.colour }} />
              {line.name && <em>{line.name}</em>}
              {line.value}
            </span>
          ))}
        </div>
      )}
      {round && width > 0 && width < KEY_BESIDE && (
        <div className="chart__legend chart__key-list">
          {(view.categories ?? []).map((label, n) => (
            <span key={label}>
              <i style={{ background: colourOf(n) }} />
              {formatLabel(label, view.x)}
              <b>{formatNumber(view.series?.[0]?.values[n] ?? 0, view.y, true)}</b>
            </span>
          ))}
        </div>
      )}
      {!round && names.length > 1 && (
        <div className="chart__legend">
          {names.map((name, n) => (
            <button
              type="button"
              key={name}
              aria-pressed={!hidden.has(name)}
              onClick={() => toggle(name)}
              title={hidden.has(name) ? `Show ${name}` : `Hide ${name}`}
            >
              <i style={{ background: colourOf(n) }} />
              {name}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
