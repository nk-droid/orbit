import { Empty } from '../components/ui.tsx'
import { useDeck } from '../lib/deck.ts'
import { timeline } from '../lib/deckView.ts'
import { useNow } from '../lib/time.ts'
import { CardFrame } from '../stage/CardFrame.tsx'

/** The rest of today and tomorrow, from the calendar. */
export function TodayCard({ id }: { id: string }) {
  const now = useNow(60_000)
  const deck = useDeck()
  const items = deck ? timeline(deck, now) : []
  const ahead = items.filter((item) => !item.kind).length

  return (
    <CardFrame id={id} title="Today" icon="calendar" tag={ahead > 0 ? `${ahead} ahead` : undefined}>
      {items.length === 0 ? (
        <Empty icon="calendar" title={deck === undefined ? 'Checking' : 'Nothing scheduled'}>
          {deck === null ? 'Orbit can’t reach its backend.' : 'Nothing on your calendar for the rest of today or tomorrow.'}
        </Empty>
      ) : (
        <ol className="tl">
          {items.map((item) => (
            <li key={item.key} className={item.kind ? `is-${item.kind}` : undefined}>
              <time>{item.time}</time>
              <div>
                {item.kind ? (
                  item.title
                ) : (
                  <>
                    <b>{item.title}</b>
                    <small>{item.sub}</small>
                  </>
                )}
              </div>
            </li>
          ))}
        </ol>
      )}
    </CardFrame>
  )
}
