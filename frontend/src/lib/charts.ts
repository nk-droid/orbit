import { useBackend } from './useBackend.ts'
import type { Loaded } from './useBackend.ts'

export type ChartKind = 'bar' | 'line' | 'area' | 'pie' | 'donut' | 'scatter' | 'histogram'

/** One axis of a chart: what it is called, and how its values are written. */
export type ChartAxis = {
  label: string
  type?: 'category' | 'time' | 'number'
  /** For dates along the bottom: the day, week, month, quarter or year each label starts. */
  per?: 'day' | 'week' | 'month' | 'quarter' | 'year' | null
  /** A currency symbol written before a number, or "%" after it. */
  unit?: string
  decimals?: number
  /** For levels in words, such as 5y+ or Expert: their names, lowest first. A value is a level's place, from 1. */
  levels?: string[]
}

export type ChartSeries = { name: string; values: (number | null)[] }
export type ChartPoint = { x: number; y: number; series?: string }
export type ChartBin = { from: number; to: number; count: number }

/**
 * A chart as the backend worked it out (backend/src/charts/build.py). Every number it
 * shows is here already: the app only draws it.
 */
export type ChartView = {
  version: 1
  chart_id?: string
  kind: ChartKind
  orientation: 'vertical' | 'horizontal'
  stacking: 'none' | 'stacked' | 'percent'
  title: string
  how?: string
  source: { from: string; label?: string; link?: string | null }
  x: ChartAxis
  y: ChartAxis
  /** Labels along the bottom, and a value for each from every series: bars, lines, areas, pies. */
  categories?: string[]
  series?: ChartSeries[]
  /** Scatter plots. */
  points?: ChartPoint[]
  series_names?: string[]
  /** Histograms. */
  bins?: ChartBin[]
  /** What was left out or changed, said on the card. */
  notes: string[]
  /** Where it is kept in Google Sheets, once saved there. */
  saved?: { title: string; tab: string; link: string } | null
  /** When its numbers were read from where they came from. */
  numbers_from?: string
}

/** A chart from the backend. A new `shownAt` fetches it again, after it changed. */
export function useChart(chartId: string, shownAt?: number): Loaded<ChartView> {
  const at = shownAt ? `?at=${shownAt}` : ''
  return useBackend<ChartView>(`/api/charts/${encodeURIComponent(chartId)}${at}`)
}
