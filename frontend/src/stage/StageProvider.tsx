import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { useVoice } from '../voice/voice-context.ts'
import { currentLayout, useLayout } from './layout.ts'
import { ALSO_CALLED, StageContext, WIDTHS } from './stage-context.ts'
import type { ArrangeRequest, ArrangeResult, CardInfo, CardSpec, OpenCard, Stage } from './stage-context.ts'

const MARGIN = 24
const CASCADE = 28
/** How much of a dragged card must stay on the stage, so it can always be grabbed again. */
const KEEP = 120

/** The card in front: of those shown, the one opened, brought back or touched last. */
function frontOf(cards: OpenCard[]): OpenCard | null {
  return cards.filter((card) => !card.back).reduce<OpenCard | null>((a, b) => (a && a.z > b.z ? a : b), null)
}

/** Without room for windows only the card in front is shown: the rest go to the background. */
function fit(cards: OpenCard[]): OpenCard[] {
  if (currentLayout() === 'windows') return cards
  const front = frontOf(cards)
  if (cards.every((card) => card.back || card === front)) return cards
  return cards.map((card) => (card.back || card === front ? card : { ...card, back: true }))
}

/**
 * The cards Orbit has pulled up: which are open, where each sits, and which are in the
 * background. The agent opens cards over the voice session; the user moves, hides and closes them.
 */
export function StageProvider({ children }: { children: ReactNode }) {
  const voice = useVoice()
  const layout = useLayout()
  const [cards, setOpen] = useState<OpenCard[]>([])
  // Every change to the cards goes through here, so what the layout has room for always holds.
  const setCards = useCallback(
    (change: (open: OpenCard[]) => OpenCard[]) => setOpen((open) => fit(change(open))),
    [],
  )
  const [waiting, setWaiting] = useState<string[]>([])
  const [shelf, setShelf] = useState<HTMLElement | null>(null)
  const layer = useRef<HTMLElement | null>(null)
  const top = useRef(10)
  const cascade = useRef(0)

  const show = useCallback((spec: CardSpec, push?: Record<string, string>) => {
    setCards((open) => {
      const existing = open.find((card) => card.spec.id === spec.id)
      // A further step of a card the user has closed does not bring it back.
      if (!existing && push) return open
      const merged = { ...existing?.spec, ...spec } as CardSpec & Record<string, unknown>
      for (const [key, entry] of Object.entries(push ?? {})) {
        merged[key] = [...((existing?.spec as Record<string, unknown> | undefined)?.[key] as string[] ?? []), entry]
      }
      if (existing) {
        // A new step of something the user put in the background stays in the background.
        const forward = push ? {} : { back: false, z: ++top.current }
        return open.map((card) => (card === existing ? { ...card, spec: merged, ...forward } : card))
      }

      const opened = { spec: merged, z: ++top.current, back: false }
      // Shown one at a time, a card needs no place yet. It is given one a step along from
      // the last, so the cards do not lie on top of each other once there is room for windows.
      if (currentLayout() !== 'windows') {
        const step = MARGIN + CASCADE * (open.length % 5)
        return [...open, { ...opened, x: step, y: step }]
      }

      // First card to the right of the orb, the second to the left; more cascade from the right.
      const width = layer.current?.clientWidth ?? window.innerWidth
      const shown = open.filter((card) => !card.back)
      const right = width - WIDTHS[spec.kind] - MARGIN
      const rightTaken = shown.some((card) => card.y === MARGIN && card.x + WIDTHS[card.spec.kind] > width / 2 + 80)
      const leftTaken = shown.some((card) => card.y === MARGIN && card.x < width / 2 - 80 - WIDTHS[card.spec.kind] / 2)
      let x = right
      let y = MARGIN
      if (rightTaken && !leftTaken) x = MARGIN
      else if (rightTaken) {
        cascade.current = (cascade.current % 4) + 1
        x = right - CASCADE * cascade.current
        y = MARGIN + CASCADE * cascade.current
      }
      return [...open, { ...opened, x: Math.max(x, 8), y }]
    })
  }, [setCards])

  const update = useCallback(
    (id: string, change: (card: OpenCard) => OpenCard) => {
      setCards((open) => open.map((card) => (card.spec.id === id ? change(card) : card)))
    },
    [setCards],
  )

  const front = useCallback((id: string) => update(id, (card) => ({ ...card, z: ++top.current })), [update])
  const toBack = useCallback((id: string) => update(id, (card) => ({ ...card, back: true })), [update])
  const restore = useCallback(
    (id: string) => update(id, (card) => ({ ...card, back: false, z: ++top.current })),
    [update],
  )
  const close = useCallback(
    (id: string) => setCards((open) => open.filter((card) => card.spec.id !== id)),
    [setCards],
  )
  const clear = useCallback(() => setCards((open) => open.map((card) => ({ ...card, back: true }))), [setCards])

  const move = useCallback(
    (id: string, x: number, y: number) => {
      const width = layer.current?.clientWidth ?? window.innerWidth
      const height = layer.current?.clientHeight ?? window.innerHeight
      update(id, (card) => ({
        ...card,
        x: Math.min(Math.max(x, KEEP - WIDTHS[card.spec.kind]), width - KEEP),
        y: Math.min(Math.max(y, 8), height - 60),
      }))
    },
    [update],
  )

  const setAwaiting = useCallback((id: string, awaiting: boolean) => {
    setWaiting((ids) => (awaiting === ids.includes(id) ? ids : awaiting ? [...ids, id] : ids.filter((i) => i !== id)))
  }, [])

  const setLayer = useCallback((el: HTMLElement | null) => {
    layer.current = el
  }, [])

  const titles = useRef(new Map<string, { title: string; tag?: string }>())
  const setTitle = useCallback((id: string, title: string | null, tag?: string) => {
    if (title === null) titles.current.delete(id)
    else titles.current.set(id, { title, tag })
  }, [])

  const shown = cards.filter((card) => !card.back)
  const frontId = frontOf(cards)?.spec.id ?? null

  // The agent asks for cards over the voice session.
  const { onShow, onArrange } = voice
  useEffect(() => onShow(show), [onShow, show])

  // The agent can also tidy the stage when the user asks it to. It needs an answer at once,
  // so this reads the latest cards from a ref instead of waiting for a render.
  const latest = useRef({ cards, frontId })
  useEffect(() => {
    latest.current = { cards, frontId }
  })
  const arrange = useCallback(({ action, which = 'all' }: ArrangeRequest): ArrangeResult => {
    const { cards: open, frontId: inFront } = latest.current
    const named = (card: OpenCard) => titles.current.get(card.spec.id)?.title ?? card.spec.kind
    const words = which.toLowerCase().split(/\s+/).filter((word) => !['the', 'my', 'card', 'cards', 'window'].includes(word))
    const matches = (card: OpenCard) => {
      if (which === 'all') return true
      if (which === 'front') return card.spec.id === inFront
      const known = `${named(card)} ${titles.current.get(card.spec.id)?.tag ?? ''} ${ALSO_CALLED[card.spec.kind]}`.toLowerCase()
      return card.spec.id === which || (words.length > 0 && words.every((word) => known.includes(word)))
    }
    const targets = open.filter(
      (card) =>
        matches(card) &&
        (action === 'close' || (action === 'background' && !card.back) || (action === 'bring_back' && card.back)),
    )
    const ids = new Set(targets.map((card) => card.spec.id))
    const after = fit(
      action === 'close'
        ? open.filter((card) => !ids.has(card.spec.id))
        : open.map((card) => (ids.has(card.spec.id) ? { ...card, back: action === 'background', z: ++top.current } : card)),
    )
    if (ids.size > 0) setCards(() => after)

    const frontAfter = frontOf(after)
    // Shown one at a time, only the last of several cards brought back ends up in front.
    const stillBack = (card: OpenCard) =>
      action === 'bring_back' && after.some((now) => now.spec.id === card.spec.id && now.back)
    const left = targets.filter(stillBack).map(named)
    return {
      ...(left.length > 0 && {
        note: `This screen shows one card at a time, so ${left.join(', ')} stayed in the background.`,
      }),
      changed: targets.filter((card) => !stillBack(card)).map(named),
      cards: after.map(
        (card): CardInfo => ({
          id: card.spec.id,
          title: named(card),
          tag: titles.current.get(card.spec.id)?.tag,
          where: card.back ? 'background' : card === frontAfter ? 'front' : 'shown',
        }),
      ),
    }
  }, [setCards])
  useEffect(() => onArrange(arrange), [onArrange, arrange])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && frontId) toBack(frontId)
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [frontId, toBack])

  // A smaller window must not leave a card stranded off the edge. Shown one at a time a
  // card has no place, and keeps the one it had for when there is room for windows again;
  // the change still goes through setCards, which sends all but the front card back.
  useEffect(() => {
    const onResize = () => setCards((open) => {
      if (currentLayout() !== 'windows') return open
      const width = layer.current?.clientWidth ?? window.innerWidth
      const height = layer.current?.clientHeight ?? window.innerHeight
      return open.map((card) => ({
        ...card,
        x: Math.max(8, Math.min(card.x, width - WIDTHS[card.spec.kind] - 8)),
        y: Math.max(8, Math.min(card.y, height - 60)),
      }))
    })
    window.addEventListener('resize', onResize)
    return () => window.removeEventListener('resize', onResize)
  }, [setCards])

  const awaiting = shown.some((card) => waiting.includes(card.spec.id))

  const stage = useMemo<Stage>(
    () => ({
      layout, cards, frontId, awaiting,
      show, front, move, toBack, restore, close, clear, setAwaiting, setTitle,
      shelf, setShelf, setLayer,
    }),
    [layout, cards, frontId, awaiting, shelf, show, front, move, toBack, restore, close, clear, setAwaiting, setTitle, setLayer],
  )

  return <StageContext.Provider value={stage}>{children}</StageContext.Provider>
}
