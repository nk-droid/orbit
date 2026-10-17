/** The orb's five visual states (see styles/orbit.css). */
export type OrbState = 'standby' | 'listening' | 'thinking' | 'speaking' | 'approval'

type Display = { orb: OrbState; label: string; hint: string }

const STANDBY: Display = { orb: 'standby', label: 'Standby', hint: 'Press the mic to talk to Orbit' }
// Standby with the wake word on: the microphone is open, listening only for Orbit's name.
const STANDBY_LISTENING: Display = { ...STANDBY, hint: 'Say “Orbit” or press the orb' }

export function standby(wake: boolean): Display {
  return wake ? STANDBY_LISTENING : STANDBY
}

// Keyed by LiveKit's agent state. Unknown states (the SDK adds them over time) fall
// back to standby rather than breaking the UI.
const BY_AGENT_STATE: Record<string, Display> = {
  connecting: { orb: 'thinking', label: 'Connecting', hint: 'Reaching Orbit…' },
  'pre-connect-buffering': { orb: 'listening', label: 'Listening', hint: 'Go ahead, I’m listening' },
  initializing: { orb: 'thinking', label: 'Waking up', hint: 'Orbit is joining…' },
  idle: { orb: 'thinking', label: 'Waking up', hint: 'Orbit is joining…' },
  listening: { orb: 'listening', label: 'Listening', hint: 'Go ahead, I’m listening' },
  thinking: { orb: 'thinking', label: 'Working', hint: 'One moment' },
  speaking: { orb: 'speaking', label: 'Speaking', hint: 'Interrupt me any time' },
  failed: { orb: 'standby', label: 'Disconnected', hint: 'The session ended unexpectedly' },
}

export function display(agentState: string, active: boolean, wake: boolean): Display {
  // With no session, any agent state is left over from the last one. LiveKit's agent hook
  // does not cancel its 20-second "agent did not join" timer when a session ends sooner,
  // so a conversation that was ended properly can still be reported as failed afterwards.
  if (!active) return standby(wake)
  return BY_AGENT_STATE[agentState] ?? standby(wake)
}
