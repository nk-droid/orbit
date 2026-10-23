import { createContext, useContext } from 'react'
import type { IconName } from '../components/Icon.tsx'
import type { Layout } from './layout.ts'

/**
 * One line of a list card: a title, a second line, small print, and a time. With a link,
 * the title opens it and a button copies it. With a Drive file's id, a button shows that
 * file in a card of its own, and with a Google Sheet's id, that sheet as a table.
 */
export type ListRow = {
  title: string
  line?: string
  detail?: string
  meta?: string
  link?: string
  file_id?: string
  sheet_id?: string
}

/**
 * Where a file that is shown as itself is kept: in Drive, in a part of an email, or with
 * the backend, which keeps the files that are to go out with an email.
 */
export type FileSource =
  | { from: 'drive'; file_id: string }
  | { from: 'mail'; message_id: string; part: string }
  | { from: 'attached'; attached_id: string }

/** Where a whole card opens, such as the Drive folder it lists, and what to call that. */
type Opens = { link?: string; link_label?: string }

/**
 * One tab of a spreadsheet, or the part of it Orbit read: some cells, or the rows with some
 * words in them. After a change, the rows or cells it changed are marked.
 */
export type SheetSource = {
  spreadsheet_id: string
  title?: string
  tab?: string
  cells?: string
  matching?: string
  changed_rows?: number[]
  changed_cells?: string[]
  /** When Orbit last showed it, so that a tab shown again after a change is fetched again. */
  shown_at?: number
}

/**
 * What a card shows. Some cards load their own data from the backend and need only an id;
 * the rest carry what the agent found.
 */
export type CardSpec =
  | { id: string; kind: 'needs' | 'today' | 'reminders' | 'connections' | 'transcript' | 'tray' }
  | { id: string; kind: 'mail'; message_id: string }
  | { id: string; kind: 'approval'; action_id: string }
  | ({ id: string; kind: 'list'; title: string; icon: IconName; tag?: string; rows: ListRow[]; more?: number; whole?: boolean } & Opens)
  | ({ id: string; kind: 'file'; title?: string; tag?: string } & FileSource)
  | ({ id: string; kind: 'sheet' } & SheetSource)
  | { id: string; kind: 'screenshot'; taken?: string; of?: string; images: string[] }
  | { id: string; kind: 'note'; title: string; html: string }
  | { id: string; kind: 'upload'; asking?: string }
  | { id: string; kind: 'browser'; name?: string; shown: boolean; profile?: string; steps?: string[] }
  /** A chart, fetched from the backend with every number worked out. Shown again after a change, it is fetched again. */
  | { id: string; kind: 'chart'; chart_id: string; title?: string; tag?: string; shown_at?: number }

export type CardKind = CardSpec['kind']

/** How wide each kind of card is as a window, in pixels. Also used to place a new card. */
export const WIDTHS: Record<CardKind, number> = {
  needs: 400,
  today: 380,
  reminders: 430,
  connections: 340,
  transcript: 420,
  mail: 460,
  approval: 400,
  list: 400,
  file: 560,
  sheet: 600,
  screenshot: 560,
  note: 520,
  tray: 400,
  upload: 380,
  browser: 360,
  chart: 560,
}

/** Other words the user might use for each kind of card, when asking Orbit to move or close it. */
export const ALSO_CALLED: Record<CardKind, string> = {
  needs: 'needs you waiting',
  today: 'today calendar schedule',
  reminders: 'reminders reminder',
  connections: 'connections links settings',
  transcript: 'conversation transcript',
  mail: 'email mail reply draft',
  approval: 'draft approval go-ahead',
  list: 'results found search',
  file: 'file document attachment',
  sheet: 'sheet spreadsheet',
  screenshot: 'screenshot screen capture',
  note: 'note answer table list summary',
  // Not "file": that is the word for a card showing one, and would close this one with it.
  tray: 'tray uploads uploaded added',
  upload: 'upload picker popup',
  browser: 'browser',
  chart: 'chart graph plot',
}

/** A card as the agent is told about it. */
export type CardInfo = { id: string; title: string; tag?: string; where: 'front' | 'shown' | 'background' }

/** What the agent may ask the stage to do with the cards. */
export type ArrangeRequest = { action: 'list' | 'background' | 'bring_back' | 'close'; which?: string }
export type ArrangeResult = { changed: string[]; cards: CardInfo[]; note?: string }

/** A card on the stage: where it sits, and whether it has been sent to the background. */
export type OpenCard = { spec: CardSpec; x: number; y: number; z: number; back: boolean }

export type Stage = {
  /** Windows around Orbit, or one card at a time where there is less room. */
  layout: Layout
  cards: OpenCard[]
  /** The card in front, which Esc sends to the background. */
  frontId: string | null
  /** A card that needs the user's go-ahead is in front of them. */
  awaiting: boolean
  /**
   * Opens a card, or brings it forward with new contents if it is already open. `push` adds
   * one entry to a list the card holds instead, and leaves a backgrounded card where it is.
   */
  show: (spec: CardSpec, push?: Record<string, string>) => void
  front: (id: string) => void
  move: (id: string, x: number, y: number) => void
  toBack: (id: string) => void
  restore: (id: string) => void
  close: (id: string) => void
  /** Sends every card to the background. */
  clear: () => void
  setAwaiting: (id: string, awaiting: boolean) => void
  /** Each card reports what it is called, so the agent can be told what is on screen. */
  setTitle: (id: string, title: string | null, tag?: string) => void
  /** Where backgrounded cards draw their chips. */
  shelf: HTMLElement | null
  setShelf: (el: HTMLElement | null) => void
  /** The stage itself, whose size is the room there is for windows. */
  setLayer: (el: HTMLElement | null) => void
}

export const StageContext = createContext<Stage | null>(null)

export function useStage(): Stage {
  const stage = useContext(StageContext)
  if (!stage) throw new Error('useStage must be used inside <StageProvider>')
  return stage
}
