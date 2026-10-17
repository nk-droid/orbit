import { Formatted } from '../components/Formatted.tsx'
import { CardFrame } from '../stage/CardFrame.tsx'
import type { CardSpec } from '../stage/stage-context.ts'

/** Something Orbit wrote for the screen instead of saying it all: a table, steps, a comparison. */
export function NoteCard({ card }: { card: Extract<CardSpec, { kind: 'note' }> }) {
  return (
    <CardFrame id={card.id} title={card.title || 'Note'} icon="orbit" pad>
      <Formatted html={card.html ?? ''} />
    </CardFrame>
  )
}
