import { useState } from 'react'
import { Chart } from '../charts/Chart.tsx'
import { NumbersTable } from '../charts/NumbersTable.tsx'
import { Icon } from '../components/Icon.tsx'
import { Empty } from '../components/ui.tsx'
import { useChart } from '../lib/charts.ts'
import { CardFrame } from '../stage/CardFrame.tsx'
import type { CardSpec } from '../stage/stage-context.ts'

type ChartSpec = Extract<CardSpec, { kind: 'chart' }>

const DAY = new Intl.DateTimeFormat(undefined, { day: 'numeric', month: 'short' })

/** When a chart's numbers were read, as its foot says it: "numbers from 3 Oct". */
function numbersFrom(when: string | undefined): string | null {
  if (!when) return null
  const read = new Date(when)
  if (Number.isNaN(read.getTime())) return null
  const today = new Date()
  return read.toDateString() === today.toDateString() ? null : `Numbers from ${DAY.format(read)}`
}

/**
 * A chart Orbit drew. Every number in it comes from the backend, worked out from the user's
 * data (backend/src/charts). It can be read as a table instead, and opens where it is kept
 * in Google Sheets once it is saved there.
 */
export default function ChartCard({ card }: { card: ChartSpec }) {
  const view = useChart(card.chart_id, card.shown_at)
  const [asNumbers, setAsNumbers] = useState(false)
  if (!view) {
    return (
      <CardFrame id={card.id} title={card.title || 'Chart'} icon="chart" tag={card.tag || undefined} pad>
        <Empty icon="chart" title={view === undefined ? 'Drawing' : 'Could not show this chart'}>
          {view === undefined ? 'Fetching the chart.' : 'It may be older than 30 days. Ask Orbit to show it again.'}
        </Empty>
      </CardFrame>
    )
  }
  const saved = view.saved?.link?.startsWith('https://') ? view.saved : null
  const older = numbersFrom(view.numbers_from)
  return (
    <CardFrame
      id={card.id}
      title={view.title}
      icon="chart"
      tag={view.source.label || card.tag || undefined}
      pad
      foot={
        <>
          <button type="button" className="btn btn--sm" onClick={() => setAsNumbers((was) => !was)} aria-pressed={asNumbers}>
            <Icon name={asNumbers ? 'chart' : 'table'} />
            {asNumbers ? 'Show chart' : 'Show numbers'}
          </button>
          {saved && (
            <a className="btn btn--sm" href={saved.link} target="_blank" rel="noopener noreferrer" title={`${saved.title}, ${saved.tab}`}>
              <Icon name="open" />
              In Sheets
            </a>
          )}
          {older && <span className="chart__when">{older}</span>}
        </>
      }
    >
      {asNumbers ? <NumbersTable view={view} /> : <Chart view={view} />}
      {view.notes.length > 0 && (
        <ul className="chart__notes">
          {view.notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      )}
    </CardFrame>
  )
}
