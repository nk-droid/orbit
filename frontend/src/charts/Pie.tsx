import type { ChartView } from '../lib/charts.ts'
import type { OnTip } from './Chart.tsx'
import { formatLabel, formatNumber } from './format.ts'
import { colourOf } from './palette.ts'

type Props = { view: ChartView; width: number; height: number; onTip: OnTip }

/** From this wide, a pie's key is drawn beside it; narrower, the card lists it below. */
export const KEY_BESIDE = 420

/** The point at an angle on a circle, from twelve o'clock, clockwise. */
function around(cx: number, cy: number, radius: number, angle: number): string {
  return `${(cx + radius * Math.sin(angle)).toFixed(2)},${(cy - radius * Math.cos(angle)).toFixed(2)}`
}

/** A slice from one angle to another, with a hole in the middle for a donut. */
function slice(cx: number, cy: number, outer: number, inner: number, from: number, to: number): string {
  // A whole circle is drawn as two halves: an arc cannot end where it starts.
  if (to - from >= Math.PI * 2 - 1e-6) {
    const half = from + Math.PI
    return slice(cx, cy, outer, inner, from, half) + slice(cx, cy, outer, inner, half, to)
  }
  const large = to - from > Math.PI ? 1 : 0
  const rim = `M${around(cx, cy, outer, from)}A${outer},${outer} 0 ${large} 1 ${around(cx, cy, outer, to)}`
  if (inner <= 0) return `${rim}L${cx},${cy}Z`
  return `${rim}L${around(cx, cy, inner, to)}A${inner},${inner} 0 ${large} 0 ${around(cx, cy, inner, from)}Z`
}

/** A pie or a donut: each label's share of the whole, with its share written on it. */
export function Pie({ view, width, height, onTip }: Props) {
  const labels = view.categories ?? []
  const values = view.series?.[0]?.values ?? []
  const whole = values.reduce<number>((sum, value) => sum + Math.max(0, value ?? 0), 0) || 1
  // The key goes beside a pie with room for it, and the pie keeps to the left.
  const wide = width >= KEY_BESIDE
  const outer = Math.max(20, Math.min(height / 2 - 12, (wide ? width * 0.45 : width) / 2 - 12))
  const inner = view.kind === 'donut' ? outer * 0.58 : 0
  const cx = wide ? outer + 16 : width / 2
  const cy = height / 2
  // Each slice starts where the one before it ended.
  const slices = labels.reduce<{ label: string; value: number; from: number; to: number; colour: string }[]>((done, label, n) => {
    const value = Math.max(0, values[n] ?? 0)
    const from = done.length ? done[done.length - 1].to : 0
    return [...done, { label, value, from, to: from + (value / whole) * Math.PI * 2, colour: colourOf(n) }]
  }, [])
  const keyX = cx + outer + 24
  const keyRow = Math.min(22, (height - 16) / Math.max(1, slices.length))
  return (
    <g>
      {slices.map((part) => {
        const share = Math.round((part.value / whole) * 100)
        const middle = (part.from + part.to) / 2
        const show = () => {
          const [x, y] = around(cx, cy, (outer + inner) / 2 || outer * 0.6, middle).split(',').map(Number)
          onTip({ x, y, title: formatLabel(part.label, view.x), lines: [{ name: '', value: `${formatNumber(part.value, view.y)} · ${share}%`, colour: part.colour }] })
        }
        const [tx, ty] = around(cx, cy, inner ? (outer + inner) / 2 : outer * 0.64, middle).split(',').map(Number)
        return (
          <g key={part.label} onMouseEnter={show} onClick={show}>
            <path className="chart__slice chart__mark" d={slice(cx, cy, outer, inner, part.from, part.to)} style={{ fill: part.colour }} />
            {share >= 6 && (
              <text className="chart__share" x={tx} y={ty} textAnchor="middle" dominantBaseline="middle">
                {share}%
              </text>
            )}
          </g>
        )
      })}
      {view.kind === 'donut' && (
        <text className="chart__whole" x={cx} y={cy} textAnchor="middle" dominantBaseline="middle">
          {formatNumber(whole, view.y, true)}
        </text>
      )}
      {wide &&
        slices.map((part, n) => (
          <g key={part.label} className="chart__key" transform={`translate(${keyX},${(height - keyRow * slices.length) / 2 + n * keyRow + keyRow / 2})`}>
            <rect x={0} y={-5} width={10} height={10} rx={2} style={{ fill: part.colour }} />
            <text x={16} y={0} dominantBaseline="middle">
              {formatLabel(part.label, view.x).slice(0, 26)}
            </text>
            <text x={width - keyX - 12} y={0} textAnchor="end" dominantBaseline="middle" className="chart__key-value">
              {formatNumber(part.value, view.y, true)}
            </text>
          </g>
        ))}
    </g>
  )
}
