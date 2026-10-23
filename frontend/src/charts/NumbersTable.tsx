import type { ChartView } from '../lib/charts.ts'
import { formatBin, formatLabel, formatNumber } from './format.ts'

/** How many rows of points are listed: past these, the table says how many more there are. */
const MAX_ROWS = 200

/** The numbers a chart draws, as a table: for reading them exactly, and for screen readers. */
export function NumbersTable({ view }: { view: ChartView }) {
  let head: string[]
  let rows: string[][]
  if (view.kind === 'histogram') {
    head = [view.x.label, 'Count']
    rows = (view.bins ?? []).map((bin) => [formatBin(bin.from, bin.to, view.x), String(bin.count)])
  } else if (view.kind === 'scatter') {
    const named = (view.series_names ?? []).length > 0
    head = [view.x.label, view.y.label, ...(named ? ['Series'] : [])]
    rows = (view.points ?? []).map((point) => [formatNumber(point.x, view.x), formatNumber(point.y, view.y), ...(named ? [point.series ?? ''] : [])])
  } else {
    const series = view.series ?? []
    head = [view.x.label, ...series.map((s) => s.name)]
    rows = (view.categories ?? []).map((label, n) => [
      formatLabel(label, view.x),
      ...series.map((s) => (s.values[n] === null ? '' : formatNumber(s.values[n] as number, view.y))),
    ])
  }
  const shown = rows.slice(0, MAX_ROWS)
  return (
    <div className="chart__numbers">
      <table>
        <thead>
          <tr>
            {head.map((name, n) => (
              <th key={n} scope="col">
                {name}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {shown.map((row, r) => (
            <tr key={r}>
              {row.map((cell, n) =>
                n === 0 ? (
                  <th key={n} scope="row">
                    {cell}
                  </th>
                ) : (
                  <td key={n}>{cell}</td>
                ),
              )}
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length > shown.length && <p className="chart__note">And {rows.length - shown.length} more.</p>}
    </div>
  )
}
