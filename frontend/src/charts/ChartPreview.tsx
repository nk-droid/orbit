import { useChart } from '../lib/charts.ts'
import { Chart } from './Chart.tsx'

/** A chart in a draft that saves it, drawn small: what will be kept in Google Sheets. */
export default function ChartPreview({ chartId }: { chartId: string }) {
  const view = useChart(chartId)
  if (!view) return null
  return (
    <div className="chart--draft">
      <Chart view={view} height={200} />
    </div>
  )
}
