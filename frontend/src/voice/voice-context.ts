import type { ReceivedMessage } from '@livekit/components-react'
import { createContext, useContext } from 'react'
import type { ArrangeRequest, ArrangeResult, CardSpec } from '../stage/stage-context.ts'
import type { OrbState } from './state.ts'

/** Answers the agent when it asks to move or close cards. */
export type ArrangeHandler = (request: ArrangeRequest) => ArrangeResult

/** Called when the agent asks for a card on screen. */
export type ShowListener = (card: CardSpec, push?: Record<string, string>) => void

/** Everything the UI needs from the live voice session, without touching LiveKit directly. */
export type Voice = {
  orb: OrbState
  label: string
  hint: string
  /** A session is connecting or connected. */
  active: boolean
  /** The agent has joined and is taking input, so typed text will reach it. */
  connected: boolean
  /** In standby and listening for Orbit's name, which starts a conversation. */
  wake: boolean
  /** When the current session connected, as epoch milliseconds. */
  startedAt: number | null
  error: string | null
  start: () => void
  end: () => void
  micEnabled: boolean
  toggleMic: () => void
  /** Spoken transcripts and typed messages, oldest first. */
  messages: ReceivedMessage[]
  send: (text: string) => void
  /** The browser is blocking audio until the user interacts. */
  audioBlocked: boolean
  startAudio: () => void
  /** Listens for cards the agent asks for. Returns how to stop listening. */
  onShow: (listener: ShowListener) => () => void
  /** Sets who answers the agent's requests to move or close cards. Returns how to stop. */
  onArrange: (handler: ArrangeHandler) => () => void
}

export const VoiceContext = createContext<Voice | null>(null)

export function useVoice(): Voice {
  const voice = useContext(VoiceContext)
  if (!voice) throw new Error('useVoice must be used inside <VoiceProvider>')
  return voice
}
