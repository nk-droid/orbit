import { useState } from 'react'
import type { ChartAxis, ChartView } from '../lib/charts.ts'
import type { OnTip } from './Chart.tsx'
import { formatBin, formatLabel, formatNumber, tickFormat } from './format.ts'
import { colourOf } from './palette.ts'
import { bands, extent, linear, niceTicks } from './scale.ts'

type Props = { view: ChartView; width: number; height: number; hidden: ReadonlySet<string>; onTip: OnTip }
type Box = { left: number; top: number; width: number; height: number }
type Drawn = { name: string; values: (number | null)[]; colour: string }
type Span = [number, number]

/** About how wide one character of a label is drawn, in pixels, at the size labels are. */
const CHAR = 6.4
/** The room each value tick wants along its axis, in pixels. */
const TICK_ROOM = 46
const MAX_LABEL_CHARS = 22

function shortened(text: string): string {
  return text.length > MAX_LABEL_CHARS ? `${text.slice(0, MAX_LABEL_CHARS - 1)}…` : text
}

function longest(texts: string[]): number {
  return texts.reduce((most, text) => Math.max(most, text.length), 0)
}

function caption(axis: ChartAxis): string {
  return axis.unit && axis.unit !== '%' ? `${axis.label} (${axis.unit})` : axis.label
}

/** Every how-manyth label is written, so that the ones written do not run into each other. */
function every(labels: string[], room: number): number {
  const wanted = longest(labels) * CHAR + 10
  return Math.max(1, Math.ceil(wanted / Math.max(1, room)))
}

/** The series drawn: all but those hidden from the legend, each keeping its own colour. */
function drawn(view: ChartView, hidden: ReadonlySet<string>): Drawn[] {
  return (view.series ?? [])
    .map((series, n) => ({ name: series.name, values: series.values, colour: colourOf(n) }))
    .filter((series) => !hidden.has(series.name))
}

/** Where each value of each series starts and ends with the series stacked, as shares of each label's whole with `percent`. */
function stacked(series: Drawn[], count: number, percent: boolean): Span[][] {
  const wholes = Array.from({ length: count }, (_, n) => series.reduce((sum, s) => sum + Math.abs(s.values[n] ?? 0), 0))
  const up = new Array<number>(count).fill(0)
  const down = new Array<number>(count).fill(0)
  return series.map((s) =>
    s.values.map((raw, n): Span => {
      let value = raw ?? 0
      if (percent) value = wholes[n] ? (value / wholes[n]) * 100 : 0
      const base = value >= 0 ? up : down
      const start = base[n]
      base[n] += value
      return [start, base[n]]
    }),
  )
}

/** The ticks of a value axis: even steps for numbers, or one for each level in words, from none. */
function valueTicks(axis: ChartAxis, low: number, high: number, count: number): number[] {
  const levels = axis.levels ?? []
  return levels.length ? [...levels.keys(), levels.length] : niceTicks(low, high, count)
}

/** The range the values span, zero included for what stands on it. */
function domain(values: number[], fromZero: boolean): [number, number] {
  const [low, high] = extent(values)
  return fromZero ? [Math.min(0, low), Math.max(0, high)] : [low, high]
}

/** What a tooltip says about one label: every series' value there. */
function tipLines(view: ChartView, series: Drawn[], index: number) {
  const whole = series.reduce((sum, s) => sum + Math.abs(s.values[index] ?? 0), 0)
  return series.map((s) => {
    const value = s.values[index]
    let written = value === null ? '—' : formatNumber(value, view.y)
    if (view.stacking === 'percent' && value !== null && whole) written += ` (${Math.round((Math.abs(value) / whole) * 100)}%)`
    return { name: series.length > 1 ? s.name : '', value: written, colour: s.colour }
  })
}

function ValueGrid({ ticks, at, box, axis, sideways, percent }: { ticks: number[]; at: (v: number) => number; box: Box; axis: ChartAxis; sideways: boolean; percent: boolean }) {
  const written = tickFormat(ticks, axis)
  const texts = ticks.map((tick) => (percent ? `${tick}%` : written(tick)))
  // Every how-manyth tick is written, the highest always, so that none run into each other.
  const room = ticks.length > 1 ? Math.abs(at(ticks[1]) - at(ticks[0])) : Infinity
  const step = sideways ? every(texts, room) : Math.max(1, Math.ceil(14 / Math.max(1, room)))
  return (
    <g className="chart__grid">
      {ticks.map((tick, n) => {
        const where = at(tick)
        const text = (ticks.length - 1 - n) % step === 0 ? texts[n] : ''
        return sideways ? (
          <g key={tick}>
            <line x1={where} x2={where} y1={box.top} y2={box.top + box.height} data-zero={tick === 0 ? '' : undefined} />
            <text x={where} y={box.top + box.height + 14} textAnchor="middle">
              {text}
            </text>
          </g>
        ) : (
          <g key={tick}>
            <line x1={box.left} x2={box.left + box.width} y1={where} y2={where} data-zero={tick === 0 ? '' : undefined} />
            <text x={box.left - 6} y={where} textAnchor="end" dominantBaseline="middle">
              {text}
            </text>
          </g>
        )
      })}
    </g>
  )
}

function Caption({ x, y, children, end }: { x: number; y: number; children: string; end?: boolean }) {
  return (
    <text className="chart__caption" x={x} y={y} textAnchor={end ? 'end' : 'start'}>
      {children}
    </text>
  )
}

/** Bars standing on the bottom: side by side for several series, or stacked. */
function Columns({ view, width, height, hidden, onTip }: Props) {
  const labels = view.categories ?? []
  const series = drawn(view, hidden)
  const percent = view.stacking === 'percent'
  const spans = view.stacking !== 'none' ? stacked(series, labels.length, percent) : null
  const [low, high] = percent ? [0, 100] : domain(spans ? spans.flat(2) : series.flatMap((s) => s.values.filter((v) => v !== null)), true)
  const ticks = valueTicks(view.y, low, high, Math.max(2, Math.floor((height - 60) / TICK_ROOM)))
  const tickTexts = ticks.map((tick) => (percent ? `${tick}%` : tickFormat(ticks, view.y)(tick)))
  const left = Math.max(30, longest(tickTexts) * CHAR + 12)
  const box: Box = { left, top: 22, width: width - left - 10, height: height - 22 - 26 }
  const y = linear([ticks[0], ticks[ticks.length - 1]], [box.top + box.height, box.top])
  const band = bands(labels.length, box.width, labels.length > 24 ? 0.12 : 0.3)
  const written = labels.map((label) => shortened(formatLabel(label, view.x, true)))
  const step = every(written, box.width / Math.max(1, labels.length))
  const tip = (index: number) => onTip({ x: box.left + band.at(index) + band.width / 2, y: box.top + 24, title: formatLabel(labels[index], view.x), lines: tipLines(view, series, index) })
  return (
    <g>
      <Caption x={box.left} y={12}>
        {caption(view.y)}
      </Caption>
      <ValueGrid ticks={ticks} at={y} box={box} axis={view.y} sideways={false} percent={percent} />
      {labels.map((label, i) => (
        <g key={`${i}:${label}`} onMouseEnter={() => tip(i)} onClick={() => tip(i)}>
          <rect className="chart__hit" x={box.left + band.at(i)} y={box.top} width={band.width} height={box.height} />
          {series.map((s, j) => {
            let x = box.left + band.at(i)
            let w = band.width
            let top: number
            let bottom: number
            if (spans) {
              const [a, b] = spans[j][i]
              top = y(Math.max(a, b))
              bottom = y(Math.min(a, b))
            } else {
              const value = s.values[i]
              if (value === null) return null
              w = band.width / series.length
              x += j * w
              top = y(Math.max(value, 0))
              bottom = y(Math.min(value, 0))
            }
            return (
              <rect
                key={s.name}
                className="chart__mark"
                x={x}
                y={top}
                width={Math.max(1, w - (spans || series.length === 1 ? 0 : 1))}
                height={Math.max(0, bottom - top)}
                style={{ fill: s.colour }}
              />
            )
          })}
          {(labels.length - 1 - i) % step === 0 && (
            <text className="chart__label" x={box.left + band.at(i) + band.width / 2} y={box.top + box.height + 16} textAnchor="middle">
              {written[i]}
            </text>
          )}
        </g>
      ))}
    </g>
  )
}

/** Bars from the left, for long labels and rankings. */
function SidewaysBars({ view, width, height, hidden, onTip }: Props) {
  const labels = view.categories ?? []
  const series = drawn(view, hidden)
  const percent = view.stacking === 'percent'
  const spans = view.stacking !== 'none' ? stacked(series, labels.length, percent) : null
  const written = labels.map((label) => shortened(formatLabel(label, view.x)))
  const left = Math.min(width * 0.4, longest(written) * CHAR + 14)
  // Levels in words are written at the end of each bar, so room is kept for them there.
  const levels = view.y.levels ?? []
  const atEnd = levels.length > 0 && series.length === 1 && !spans
  const endRoom = atEnd ? longest(levels) * CHAR + 10 : 0
  const box: Box = { left, top: 20, width: width - left - 18 - endRoom, height: height - 20 - 24 }
  const [low, high] = percent ? [0, 100] : domain(spans ? spans.flat(2) : series.flatMap((s) => s.values.filter((v) => v !== null)), true)
  const ticks = valueTicks(view.y, low, high, Math.max(2, Math.floor(box.width / 80)))
  const x = linear([ticks[0], ticks[ticks.length - 1]], [box.left, box.left + box.width])
  const band = bands(labels.length, box.height, 0.25)
  const tip = (index: number) => onTip({ x: box.left + box.width / 2, y: box.top + band.at(index) + band.width / 2, title: formatLabel(labels[index], view.x), lines: tipLines(view, series, index) })
  return (
    <g>
      <Caption x={width - 18} y={12} end>
        {caption(view.y)}
      </Caption>
      <ValueGrid ticks={ticks} at={x} box={box} axis={view.y} sideways percent={percent} />
      {labels.map((label, i) => (
        <g key={`${i}:${label}`} onMouseEnter={() => tip(i)} onClick={() => tip(i)}>
          <rect className="chart__hit" x={box.left} y={box.top + band.at(i)} width={box.width} height={band.width} />
          {series.map((s, j) => {
            let y = box.top + band.at(i)
            let h = band.width
            let from: number
            let to: number
            if (spans) {
              const [a, b] = spans[j][i]
              from = x(Math.min(a, b))
              to = x(Math.max(a, b))
            } else {
              const value = s.values[i]
              if (value === null) return null
              h = band.width / series.length
              y += j * h
              from = x(Math.min(value, 0))
              to = x(Math.max(value, 0))
            }
            return (
              <g key={s.name}>
                <rect className="chart__mark" x={from} y={y} width={Math.max(0, to - from)} height={Math.max(1, h - (spans || series.length === 1 ? 0 : 1))} style={{ fill: s.colour }} />
                {atEnd && s.values[i] !== null && (
                  <text className="chart__value" x={to + 6} y={y + h / 2} dominantBaseline="middle">
                    {formatNumber(s.values[i] as number, view.y)}
                  </text>
                )}
              </g>
            )
          })}
          <text className="chart__label" x={box.left - 8} y={box.top + band.at(i) + band.width / 2} textAnchor="end" dominantBaseline="middle">
            {written[i]}
          </text>
        </g>
      ))}
    </g>
  )
}

/** Lines along an ordered axis, or the areas under them, stacked or not. */
function Lines({ view, width, height, hidden, onTip }: Props) {
  const [over, setOver] = useState<number | null>(null)
  const labels = view.categories ?? []
  const series = drawn(view, hidden)
  const area = view.kind === 'area'
  const percent = view.stacking === 'percent'
  const spans = area && view.stacking !== 'none' ? stacked(series, labels.length, percent) : null
  const [low, high] = percent ? [0, 100] : domain(spans ? spans.flat(2) : series.flatMap((s) => s.values.filter((v) => v !== null)), area)
  const ticks = valueTicks(view.y, low, high, Math.max(2, Math.floor((height - 60) / TICK_ROOM)))
  const tickTexts = ticks.map((tick) => (percent ? `${tick}%` : tickFormat(ticks, view.y)(tick)))
  const left = Math.max(30, longest(tickTexts) * CHAR + 12)
  const box: Box = { left, top: 22, width: width - left - 14, height: height - 22 - 26 }
  const y = linear([ticks[0], ticks[ticks.length - 1]], [box.top + box.height, box.top])
  const at = (index: number) => box.left + (labels.length === 1 ? box.width / 2 : (index * box.width) / (labels.length - 1))
  const written = labels.map((label) => shortened(formatLabel(label, view.x, true)))
  const step = every(written, box.width / Math.max(1, labels.length - 1))
  const column = box.width / Math.max(1, labels.length)
  const marked = labels.length <= 40

  /** A line through the values, broken where one is missing. */
  const through = (values: (number | null)[]) => {
    let path = ''
    let drawing = false
    values.forEach((value, n) => {
      if (value === null) {
        drawing = false
        return
      }
      path += `${drawing ? 'L' : 'M'}${at(n).toFixed(1)},${y(value).toFixed(1)}`
      drawing = true
    })
    return path
  }
  const tip = (index: number) => {
    setOver(index)
    onTip({ x: at(index), y: box.top + 24, title: formatLabel(labels[index], view.x), lines: tipLines(view, series, index) })
  }
  return (
    <g onMouseLeave={() => setOver(null)}>
      <Caption x={box.left} y={12}>
        {caption(view.y)}
      </Caption>
      <ValueGrid ticks={ticks} at={y} box={box} axis={view.y} sideways={false} percent={percent} />
      {series.map((s, j) => {
        if (spans) {
          const tops = spans[j].map(([, end], n) => `${at(n).toFixed(1)},${y(end).toFixed(1)}`)
          const bottoms = spans[j].map(([start], n) => `${at(n).toFixed(1)},${y(start).toFixed(1)}`).reverse()
          return (
            <g key={s.name}>
              <polygon className="chart__area" points={[...tops, ...bottoms].join(' ')} style={{ fill: s.colour }} />
              <polyline className="chart__line" points={tops.join(' ')} style={{ stroke: s.colour }} />
            </g>
          )
        }
        const line = through(s.values)
        const zero = y(Math.max(ticks[0], Math.min(0, ticks[ticks.length - 1])))
        const filled = area
          ? s.values.reduce<string>((path, value, n) => {
              if (value === null) return path
              const start = path === '' || s.values[n - 1] === null
              return `${path}${start ? `M${at(n).toFixed(1)},${zero.toFixed(1)}` : ''}L${at(n).toFixed(1)},${y(value).toFixed(1)}${n === s.values.length - 1 || s.values[n + 1] === null ? `L${at(n).toFixed(1)},${zero.toFixed(1)}Z` : ''}`
            }, '')
          : ''
        return (
          <g key={s.name}>
            {area && <path className="chart__area" d={filled} style={{ fill: s.colour }} />}
            <path className="chart__line" d={line} style={{ stroke: s.colour }} />
            {marked &&
              s.values.map((value, n) =>
                value === null ? null : <circle key={n} className="chart__dot" cx={at(n)} cy={y(value)} r={over === n ? 4.5 : 2.5} style={{ fill: s.colour }} />,
              )}
          </g>
        )
      })}
      {over !== null && <line className="chart__guide" x1={at(over)} x2={at(over)} y1={box.top} y2={box.top + box.height} />}
      {labels.map((label, i) => (
        <g key={`${i}:${label}`}>
          <rect className="chart__hit" x={at(i) - column / 2} y={box.top} width={column} height={box.height} onMouseEnter={() => tip(i)} onClick={() => tip(i)} />
          {(labels.length - 1 - i) % step === 0 && (
            <text className="chart__label" x={at(i)} y={box.top + box.height + 16} textAnchor="middle">
              {written[i]}
            </text>
          )}
        </g>
      ))}
    </g>
  )
}

/** Points of two numbers, coloured by their series. */
function Scatter({ view, width, height, hidden, onTip }: Props) {
  const names = view.series_names ?? []
  const points = (view.points ?? []).filter((point) => !point.series || !hidden.has(point.series))
  const [xLow, xHigh] = extent(points.map((p) => p.x))
  const [yLow, yHigh] = extent(points.map((p) => p.y))
  const yTicks = niceTicks(yLow, yHigh, Math.max(2, Math.floor((height - 70) / TICK_ROOM)))
  const yTexts = yTicks.map(tickFormat(yTicks, view.y))
  const left = Math.max(30, longest(yTexts) * CHAR + 12)
  const box: Box = { left, top: 22, width: width - left - 14, height: height - 22 - 40 }
  const xTicks = niceTicks(xLow, xHigh, Math.max(2, Math.floor(box.width / 80)))
  const x = linear([xTicks[0], xTicks[xTicks.length - 1]], [box.left, box.left + box.width])
  const y = linear([yTicks[0], yTicks[yTicks.length - 1]], [box.top + box.height, box.top])
  const radius = points.length > 500 ? 2 : 3.5
  return (
    <g>
      <Caption x={box.left} y={12}>
        {caption(view.y)}
      </Caption>
      <Caption x={box.left + box.width} y={height - 4} end>
        {caption(view.x)}
      </Caption>
      <ValueGrid ticks={yTicks} at={y} box={box} axis={view.y} sideways={false} percent={false} />
      <g className="chart__grid">
        {xTicks.map((tick) => (
          <text key={tick} x={x(tick)} y={box.top + box.height + 14} textAnchor="middle">
            {tickFormat(xTicks, view.x)(tick)}
          </text>
        ))}
      </g>
      {points.map((point, n) => {
        const colour = colourOf(point.series ? Math.max(0, names.indexOf(point.series)) : 0)
        const show = () =>
          onTip({
            x: x(point.x),
            y: y(point.y),
            title: point.series ?? view.title,
            lines: [
              { name: view.x.label, value: formatNumber(point.x, view.x), colour },
              { name: view.y.label, value: formatNumber(point.y, view.y), colour },
            ],
          })
        return <circle key={n} className="chart__dot chart__mark" cx={x(point.x)} cy={y(point.y)} r={radius} style={{ fill: colour }} onMouseEnter={show} onClick={show} />
      })}
    </g>
  )
}

/** How values spread: the backend's bins, side by side. */
function Histogram({ view, width, height, onTip }: Props) {
  const bins = view.bins ?? []
  const ticks = niceTicks(0, Math.max(1, ...bins.map((bin) => bin.count)), Math.max(2, Math.floor((height - 60) / TICK_ROOM)))
  const left = Math.max(30, longest(ticks.map(String)) * CHAR + 12)
  const box: Box = { left, top: 22, width: width - left - 14, height: height - 22 - 40 }
  const from = bins[0]?.from ?? 0
  const to = bins[bins.length - 1]?.to ?? 1
  const x = linear([from, to], [box.left, box.left + box.width])
  const y = linear([ticks[0], ticks[ticks.length - 1]], [box.top + box.height, box.top])
  const edges = [...bins.map((bin) => bin.from), to]
  const edgeText = tickFormat(edges, view.x)
  const step = every(edges.map(edgeText), box.width / Math.max(1, bins.length))
  const countAxis: ChartAxis = { label: 'Count', decimals: 0 }
  return (
    <g>
      <Caption x={box.left} y={12}>
        Count
      </Caption>
      <Caption x={box.left + box.width} y={height - 4} end>
        {caption(view.x)}
      </Caption>
      <ValueGrid ticks={ticks} at={y} box={box} axis={countAxis} sideways={false} percent={false} />
      {bins.map((bin, n) => {
        const show = () =>
          onTip({
            x: (x(bin.from) + x(bin.to)) / 2,
            y: y(bin.count),
            title: formatBin(bin.from, bin.to, view.x),
            lines: [{ name: '', value: `${bin.count}`, colour: colourOf(0) }],
          })
        return (
          <g key={n} onMouseEnter={show} onClick={show}>
            <rect className="chart__hit" x={x(bin.from)} y={box.top} width={x(bin.to) - x(bin.from)} height={box.height} />
            <rect className="chart__mark" x={x(bin.from) + 0.5} y={y(bin.count)} width={Math.max(1, x(bin.to) - x(bin.from) - 1)} height={Math.max(0, y(0) - y(bin.count))} style={{ fill: colourOf(0) }} />
          </g>
        )
      })}
      <g className="chart__grid">
        {edges.map((edge, n) =>
          (edges.length - 1 - n) % step === 0 ? (
            <text key={n} x={x(edge)} y={box.top + box.height + 14} textAnchor="middle">
              {edgeText(edge)}
            </text>
          ) : null,
        )}
      </g>
    </g>
  )
}

/** A chart with axes: bars, lines, areas, points or bins. */
export function Cartesian(props: Props) {
  switch (props.view.kind) {
    case 'scatter':
      return <Scatter {...props} />
    case 'histogram':
      return <Histogram {...props} />
    case 'bar':
      return props.view.orientation === 'horizontal' ? <SidewaysBars {...props} /> : <Columns {...props} />
    default:
      return <Lines {...props} />
  }
}
