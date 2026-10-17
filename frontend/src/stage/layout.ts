import { useSyncExternalStore } from 'react'

/**
 * How the stage is arranged, which follows the size of the window.
 *
 * - `windows`: cards float around Orbit and can be dragged. Needs a desktop-sized window.
 * - `stacked`: one card at a time, below Orbit. A phone held upright, or a narrow window.
 * - `beside`: one card at a time, next to Orbit. A phone on its side: short but wide.
 *
 * The styles follow the same value, as `data-layout` on the app (styles/stage.css).
 */
export type Layout = 'windows' | 'stacked' | 'beside'

const NARROW = window.matchMedia('(max-width: 899px)')
const SHORT = window.matchMedia('(max-height: 499px)')
// Wide enough to put a card next to Orbit instead of below it.
const ROOM_BESIDE = window.matchMedia('(min-width: 560px)')

/** The layout right now. Safe to call anywhere, such as inside a state update. */
export function currentLayout(): Layout {
  if (SHORT.matches) return ROOM_BESIDE.matches ? 'beside' : 'stacked'
  return NARROW.matches ? 'stacked' : 'windows'
}

function subscribe(onChange: () => void) {
  const queries = [NARROW, SHORT, ROOM_BESIDE]
  for (const query of queries) query.addEventListener('change', onChange)
  return () => {
    for (const query of queries) query.removeEventListener('change', onChange)
  }
}

/** The layout, kept current as the window is resized or the device is turned. */
export function useLayout(): Layout {
  return useSyncExternalStore(subscribe, currentLayout)
}
