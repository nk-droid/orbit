import type { ChartAxis } from '../lib/charts.ts'

/**
 * A number as an axis writes it, with its unit. On a tick it is written short, and with
 * `compact` as "12K": every tick of an axis is written the same way (tickFormat).
 */
export function formatNumber(value: number, axis: ChartAxis, onTick = false, compact = onTick && Math.abs(value) >= 10_000): string {
  // A level in words, at its place on the scale.
  if (axis.levels?.length) return axis.levels[Math.round(value) - 1] ?? ''
  const decimals = axis.decimals ?? 0
  const written = new Intl.NumberFormat(undefined, {
    notation: compact ? 'compact' : 'standard',
    minimumFractionDigits: onTick ? 0 : decimals,
    maximumFractionDigits: onTick ? (compact ? 1 : Math.max(decimals, 2)) : decimals,
  }).format(value)
  const unit = axis.unit ?? ''
  if (!unit) return written
  if (unit === '%') return `${written}%`
  // The minus goes before the currency: -₹45, not ₹-45.
  return written.startsWith('-') ? `-${unit}${written.slice(1)}` : `${unit}${written}`
}

/** How an axis writes its ticks: all of them short ("12K") once any is that big. */
export function tickFormat(ticks: number[], axis: ChartAxis): (value: number) => string {
  if (axis.levels?.length) return (value) => formatNumber(value, axis)
  const compact = ticks.some((tick) => Math.abs(tick) >= 10_000)
  return (value) => formatNumber(value, axis, true, compact)
}

const MONTH = new Intl.DateTimeFormat(undefined, { month: 'short', timeZone: 'UTC' })

/**
 * A label along the bottom as it is read. A date is the start of its day, week, month,
 * quarter or year, and is written as one: "3 Oct", "Oct 2026", "Q4 2026", "2026".
 */
export function formatLabel(label: string, axis: ChartAxis, short = false): string {
  if (axis.type !== 'time') return label
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(label)
  if (!match) return label
  const moment = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])))
  const year = match[1]
  switch (axis.per) {
    case 'year':
      return year
    case 'quarter':
      return `Q${Math.floor(moment.getUTCMonth() / 3) + 1}${short ? '' : ` ${year}`}`
    case 'month':
      return short ? MONTH.format(moment) : `${MONTH.format(moment)} ${year}`
    default:
      return `${moment.getUTCDate()} ${MONTH.format(moment)}${short ? '' : ` ${year}`}`
  }
}

/** A histogram's bin as it is read: "10–20". */
export function formatBin(from: number, to: number, axis: ChartAxis): string {
  return `${formatNumber(from, { ...axis, decimals: undefined }, true)}–${formatNumber(to, { ...axis, decimals: undefined }, true)}`
}
