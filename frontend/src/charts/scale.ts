/**
 * The little arithmetic a chart needs to draw what the backend worked out: even ticks
 * along an axis, and where a value or a label falls on the page.
 */

/** A step of 1, 2, 2.5 or 5 times a power of ten, no smaller than `raw`. */
export function niceStep(raw: number): number {
  if (!(raw > 0)) return 1
  const power = 10 ** Math.floor(Math.log10(raw))
  for (const factor of [1, 2, 2.5, 5, 10]) {
    if (factor * power >= raw - 1e-12) return factor * power
  }
  return 10 * power
}

/**
 * Even ticks from at or below `low` to at or above `high`, about `count` of them. Zero is
 * always among them when the values reach it, so bars stand on it.
 */
export function niceTicks(low: number, high: number, count = 5): number[] {
  if (low === high) {
    if (low === 0) return [0, 1]
    low = Math.min(0, low)
    high = Math.max(0, high)
  }
  const step = niceStep((high - low) / Math.max(1, count))
  const first = Math.floor(low / step + 1e-9) * step
  const last = Math.ceil(high / step - 1e-9) * step
  const ticks: number[] = []
  for (let n = 0; first + n * step <= last + step / 2; n++) {
    // Rounded, so 0.30000000000000004 is written 0.3.
    ticks.push(Number((first + n * step).toPrecision(12)))
  }
  return ticks
}

/** A straight scale: from a domain of values onto a range on the page. */
export function linear(domain: [number, number], range: [number, number]): (value: number) => number {
  const [d0, d1] = domain
  const [r0, r1] = range
  const span = d1 - d0 || 1
  return (value) => r0 + ((value - d0) / span) * (r1 - r0)
}

/** Places for labels side by side: where each band starts, and how wide it is. */
export function bands(count: number, length: number, padding = 0.2): { at: (index: number) => number; width: number } {
  const step = length / Math.max(1, count)
  const width = step * (1 - padding)
  return { at: (index) => index * step + (step - width) / 2, width }
}

/** The lowest and highest of some numbers, nulls left out. [0, 0] for none. */
export function extent(values: (number | null | undefined)[]): [number, number] {
  let low = Infinity
  let high = -Infinity
  for (const value of values) {
    if (value === null || value === undefined || Number.isNaN(value)) continue
    low = Math.min(low, value)
    high = Math.max(high, value)
  }
  return low <= high ? [low, high] : [0, 0]
}
