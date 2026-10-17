import type { SheetSource } from '../stage/stage-context.ts'
import { useBackend } from './useBackend.ts'
import type { Loaded } from './useBackend.ts'

/** One tab of a spreadsheet as the backend reads it (backend/src/api/sheets.py). */
export type SheetView = {
  id: string
  title: string
  /** Where the spreadsheet opens in Google Sheets. */
  link: string | null
  tabs: string[]
  tab: string
  /** The column the cells start at, counted from 0: 0 is A. */
  first_column: number
  /** The row of headers, or null when the tab has none. */
  header_row: number | null
  headers: string[]
  /** The rows with anything in them, each with the number the sheet gives it. */
  rows: { row: number; cells: string[] }[]
  /** Rows found but not shown here. */
  more: number
  /** False when the tab has more rows than the backend reads at once. */
  complete: boolean
}

/** The letters a column is named by, from its place counted from 0: 0 is A, 26 is AA. */
export function columnLetter(index: number): string {
  let name = ''
  for (let n = index + 1; n > 0; n = Math.floor((n - 1) / 26)) name = String.fromCharCode(65 + ((n - 1) % 26)) + name
  return name
}

/**
 * A tab of a spreadsheet. Orbit's choice of cells or of matching rows holds only for the tab
 * Orbit showed: `own` is false for a tab the user picked on the card, which is shown whole.
 */
export function useSheet(source: SheetSource, tab: string, own: boolean): Loaded<SheetView> {
  const query = new URLSearchParams({ tab })
  if (own && source.cells) query.set('cells', source.cells)
  if (own && source.matching) query.set('matching', source.matching)
  // Not read by the backend: it makes the same tab, shown again after a change, a new fetch.
  if (source.shown_at) query.set('at', String(source.shown_at))
  return useBackend<SheetView>(`/api/sheets/${encodeURIComponent(source.spreadsheet_id)}?${query}`)
}
