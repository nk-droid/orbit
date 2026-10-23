/**
 * The colours of a chart's series. The theme's own come first: accent, warm and ok. The
 * rest are chosen to stay apart from them and from each other on the dark background,
 * for colour-blind eyes too: bright and dark, warm and cool alternate. The alert colour
 * is kept out, since red reads as a warning.
 */
const COLOURS = [
  'rgb(var(--accent-rgb))',
  'rgb(var(--warm-rgb))',
  'rgb(var(--ok-rgb))',
  '#b18cff',
  '#ff7eb6',
  '#e3f07a',
  '#5c8dff',
  '#9fb3c4',
]

/** The colour of the series at this place, repeating past the last. */
export function colourOf(index: number): string {
  return COLOURS[index % COLOURS.length]
}
