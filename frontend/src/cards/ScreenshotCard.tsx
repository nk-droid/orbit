import { CardFrame } from '../stage/CardFrame.tsx'
import type { CardSpec } from '../stage/stage-context.ts'

// Only a picture Orbit took is drawn: a JPEG carried in the card itself.
const TAKEN = /^data:image\/jpeg;base64,[A-Za-z0-9+/]+=*$/

/**
 * A screenshot Orbit took when the user asked: a picture of each screen, or of the one
 * window they named. What Orbit looked at. It travels in the card and is kept nowhere else.
 */
export function ScreenshotCard({ card }: { card: Extract<CardSpec, { kind: 'screenshot' }> }) {
  const images = (card.images ?? []).filter((image) => TAKEN.test(image))
  return (
    <CardFrame id={card.id} title="Screenshot" icon="image" tag={card.taken} pad>
      {card.of && <p className="shot__of">Only {card.of}.</p>}
      {images.map((image, n) => (
        <figure className="shot" key={n}>
          {images.length > 1 && <figcaption className="label">Screen {n + 1}</figcaption>}
          <img className="shot__img" src={image} alt={images.length > 1 ? `Screen ${n + 1}` : 'Your screen'} />
        </figure>
      ))}
      <p className="file__note">What Orbit looked at, when you asked. It is not kept once the conversation ends.</p>
    </CardFrame>
  )
}
