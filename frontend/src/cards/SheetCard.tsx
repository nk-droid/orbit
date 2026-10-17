import { useEffect, useRef, useState } from 'react'
import { Icon } from '../components/Icon.tsx'
import { Empty } from '../components/ui.tsx'
import { columnLetter, useSheet } from '../lib/sheets.ts'
import type { SheetView } from '../lib/sheets.ts'
import { CardFrame } from '../stage/CardFrame.tsx'
import type { CardSpec } from '../stage/stage-context.ts'
import { useStage } from '../stage/stage-context.ts'
import { OpenFoot } from './FoundCards.tsx'

type SheetSpec = Extract<CardSpec, { kind: 'sheet' }>
type Line = { row: number; cells: string[]; head?: boolean }

/** The rows to draw in the sheet's own order, its headers among them in their place. */
function linesOf(sheet: SheetView): Line[] {
  const lines: Line[] = [...sheet.rows]
  if (sheet.header_row !== null) lines.push({ row: sheet.header_row, cells: sheet.headers, head: true })
  return lines.sort((a, b) => a.row - b.row)
}

/**
 * One tab of a Google Sheet, drawn as Sheets draws it: column letters along the top and row
 * numbers down the side, so that a cell Orbit names, such as D7, can be found. The cells are
 * fetched from the backend, so the card shows the sheet as it is now. What a change just did
 * is marked, and the spreadsheet's other tabs can be looked at from here.
 */
export function SheetCard({ card }: { card: SheetSpec }) {
  const stage = useStage()
  // A tab picked here holds until Orbit shows the sheet again.
  const [picked, setPicked] = useState<{ tab: string; shownAt?: number } | null>(null)
  const pickedTab = picked && picked.shownAt === card.shown_at ? picked.tab : null
  const sheet = useSheet(card, pickedTab ?? card.tab ?? '', pickedTab === null)
  const marked = useRef<HTMLTableRowElement>(null)

  // What a change did is brought into view once it is drawn, below the letters and headers
  // that stay at the top. Only the card scrolls: nothing around it moves.
  useEffect(() => {
    const row = marked.current
    const body = row?.closest('.panel__body')
    if (!row || !body) return
    const top = row.closest('table')?.tHead?.offsetHeight ?? 0
    const shown = body.getBoundingClientRect()
    const at = row.getBoundingClientRect()
    if (at.top - shown.top < top) body.scrollTop -= top - (at.top - shown.top)
    else if (at.bottom > shown.bottom) body.scrollTop += at.bottom - shown.bottom
  }, [sheet])

  if (!sheet) {
    // Drive can still show a sheet as pages when its cells cannot be had, as before the
    // Google Sheets API is enabled for Orbit.
    const asPages = () =>
      stage.show({ id: `file:${card.spreadsheet_id}`, kind: 'file', from: 'drive', file_id: card.spreadsheet_id, title: card.title })
    return (
      <CardFrame
        id={card.id}
        title={card.title || 'Sheet'}
        icon="table"
        tag={card.tab || undefined}
        foot={
          sheet === null && (
            <button type="button" className="btn btn--sm" onClick={asPages}>
              <Icon name="file" />
              Show its pages instead
            </button>
          )
        }
      >
        <Empty icon="table" title={sheet === undefined ? 'Opening' : 'Could not open this sheet'}>
          {sheet === undefined
            ? 'Fetching the sheet.'
            : 'It may have been moved or deleted, Google may not be connected, or the Google Sheets API may not be enabled for Orbit yet.'}
        </Empty>
      </CardFrame>
    )
  }

  // What Orbit chose to show, and the marks of its change, belong to the tab Orbit showed.
  const asShown = pickedTab === null
  const changedRows = new Set(asShown ? card.changed_rows : [])
  const changedCells = new Set(asShown ? card.changed_cells : [])
  const narrowed = asShown && (card.matching || card.cells)
  const lines = linesOf(sheet)
  const width = Math.max(sheet.headers.length, ...lines.map((line) => line.cells.length))
  const letters = Array.from({ length: width }, (_, n) => columnLetter(sheet.first_column + n))
  const isMarked = (line: Line) =>
    changedRows.has(line.row) || letters.some((letter) => changedCells.has(`${letter}${line.row}`))
  const firstMarked = lines.find(isMarked)?.row
  // Headers above everything else stay in view with the column letters as the rows scroll.
  const headOnTop = lines[0]?.head === true
  const drawn = (line: Line) => (
    <tr
      key={line.row}
      ref={line.row === firstMarked ? marked : undefined}
      className={line.head ? 'sheet__head' : undefined}
      data-changed={changedRows.has(line.row) ? '' : undefined}
    >
      <th scope="row">{line.row}</th>
      {letters.map((letter, n) => (
        <td key={letter} data-changed={changedCells.has(`${letter}${line.row}`) ? '' : undefined}>
          <div className="sheet__cell">{line.cells[n] ?? ''}</div>
        </td>
      ))}
    </tr>
  )
  const link = sheet.link?.startsWith('https://') ? sheet.link : null
  return (
    <CardFrame
      id={card.id}
      title={sheet.title}
      icon="table"
      tag={sheet.tab}
      foot={link && <OpenFoot link={link} label="Open in Sheets" />}
    >
      {sheet.tabs.length > 1 && (
        <div className="sheet__tabs" role="tablist" aria-label="Tabs">
          {sheet.tabs.map((name) => (
            <button
              type="button"
              role="tab"
              className="sheet__tab"
              aria-selected={name === sheet.tab}
              key={name}
              onClick={() => setPicked({ tab: name, shownAt: card.shown_at })}
            >
              {name}
            </button>
          ))}
        </div>
      )}
      {narrowed && (
        <p className="sheet__note">{card.matching ? `The rows with “${card.matching}” in them.` : `Cells ${card.cells}.`}</p>
      )}
      {lines.length === 0 ? (
        <Empty icon="table" title="Nothing here">
          {narrowed ? 'No rows match.' : 'This tab is empty.'}
        </Empty>
      ) : (
        <div className="sheet">
          <table>
            <thead>
              <tr className="sheet__letters">
                <th className="sheet__corner" aria-label="Row" />
                {letters.map((letter) => (
                  <th scope="col" key={letter}>
                    {letter}
                  </th>
                ))}
              </tr>
              {headOnTop && drawn(lines[0])}
            </thead>
            <tbody>{(headOnTop ? lines.slice(1) : lines).map(drawn)}</tbody>
          </table>
        </div>
      )}
      {sheet.more > 0 && <p className="sheet__note">And {sheet.more} more rows. Open it in Sheets to see them all.</p>}
      {!sheet.complete && <p className="sheet__note">This tab is longer than Orbit reads: only its first 10,000 rows are here.</p>}
    </CardFrame>
  )
}
