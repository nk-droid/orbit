/** Colour carries state, not source: amber needs you, cyan is Orbit, red is blocked, green is done. */
export type Tone = 'warm' | 'accent' | 'alert' | 'ok' | 'mute'

export type TagData = { tone?: Tone; label: string }
