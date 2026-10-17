import {
  RoomAudioRenderer,
  SessionEvent,
  SessionProvider,
  useAgent,
  useAudioPlayback,
  useEvents,
  useSession,
  useSessionMessages,
  useTrackToggle,
} from '@livekit/components-react'
import type { UseSessionReturn } from '@livekit/components-react'
import { ConnectionState, Room, TokenSource, Track } from 'livekit-client'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { apiUrl } from '../lib/api.ts'
import { AGENT_METADATA, useAppSessionAlive } from '../lib/appSession.ts'
import { HEAR_ONLY_THE_ROOM } from './microphone.ts'
import { display, standby } from './state.ts'
import { useWakeWord } from './useWakeWord.ts'
import { VoiceContext } from './voice-context.ts'
import type { CardSpec } from '../stage/stage-context.ts'
import type { ArrangeHandler, ShowListener, Voice } from './voice-context.ts'

// Must match the name the agent registers under in backend/src/agent.py.
const AGENT_NAME = 'orbit'

const tokenSource = TokenSource.endpoint(apiUrl('/api/token'))

// The agent states in which it is in the room and taking input. Before these, speech is
// buffered for it but typed text would be lost.
const READY = new Set(['listening', 'thinking', 'speaking'])

// The topic the agent's tools publish cards on. Must match backend/src/agent_tools/stage.py.
const STAGE_TOPIC = 'orbit.stage'
// The function the agent calls to move or close cards, and to learn what is on screen.
const CARDS_METHOD = 'orbit.cards'
// How long sound has to stay blocked before the user is asked to turn it on.
const BLOCKED_AFTER_MS = 1500

/** Owns the LiveKit session for the whole app, so a conversation survives navigation. */
export function VoiceProvider({ children }: { children: ReactNode }) {
  // The room is made here, not by the session, so the microphone can be opened Orbit's way.
  const [room] = useState(() => new Room({ audioCaptureDefaults: HEAR_ONLY_THE_ROOM }))
  // Each conversation is told this tab's session, so Orbit remembers what was said in it.
  const session = useSession(tokenSource, { agentName: AGENT_NAME, agentMetadata: AGENT_METADATA, room })
  useAppSessionAlive()
  return (
    <SessionProvider session={session}>
      <VoiceState session={session}>{children}</VoiceState>
      <RoomAudioRenderer />
    </SessionProvider>
  )
}

function VoiceState({ session, children }: { session: UseSessionReturn; children: ReactNode }) {
  const agent = useAgent(session)
  const { messages, send } = useSessionMessages(session)
  const mic = useTrackToggle({ source: Track.Source.Microphone, room: session.room })
  const audio = useAudioPlayback(session.room)
  const [startError, setStartError] = useState<string | null>(null)
  const [startedAt, setStartedAt] = useState<number | null>(null)
  // Orbit has said it is standing by, so its leaving is the end of the conversation, not a fault.
  const [leaving, setLeaving] = useState(false)

  const { start: startSession, end: endSession, isConnected } = session
  const active = session.connectionState !== ConnectionState.Disconnected

  useEvents(
    session,
    SessionEvent.ConnectionStateChanged,
    (state) => {
      // Reconnects keep the original start time.
      if (state === ConnectionState.Connected) setStartedAt((at) => at ?? Date.now())
      else if (state === ConnectionState.Disconnected) {
        setStartedAt(null)
        setLeaving(false)
      }
    },
    [],
  )

  useEvents(
    session,
    SessionEvent.MediaDevicesError,
    () => setStartError('Orbit can’t use the microphone. Allow it for this site, then try again.'),
    [],
  )

  const start = useCallback(() => {
    setStartError(null)
    startSession().catch(() => {
      setStartError('Couldn’t start a session. Check that the Orbit backend is running.')
    })
  }, [startSession])

  const end = useCallback(() => {
    void endSession()
  }, [endSession])

  // In standby, saying Orbit's name starts a conversation, as pressing the orb does.
  const wake = useWakeWord(!active, start)

  // The agent asks for cards over a text stream; whoever is listening puts them on screen.
  const listeners = useRef(new Set<ShowListener>())
  const onShow = useCallback((listener: ShowListener) => {
    listeners.current.add(listener)
    return () => void listeners.current.delete(listener)
  }, [])

  const { room } = session
  useEffect(() => {
    room.registerTextStreamHandler(STAGE_TOPIC, async (reader) => {
      try {
        const message: unknown = JSON.parse(await reader.readAll())
        if (isStandby(message)) setLeaving(true)
        else if (isShow(message)) listeners.current.forEach((listener) => listener(message.card, message.push))
      } catch {
        // A stream cut short or a malformed message is dropped: the spoken answer still stands.
      }
    })
    return () => room.unregisterTextStreamHandler(STAGE_TOPIC)
  }, [room])

  const arrange = useRef<ArrangeHandler | null>(null)
  const onArrange = useCallback((handler: ArrangeHandler) => {
    arrange.current = handler
    return () => {
      if (arrange.current === handler) arrange.current = null
    }
  }, [])

  useEffect(() => {
    room.registerRpcMethod(CARDS_METHOD, async (data) =>
      JSON.stringify(arrange.current?.(JSON.parse(data.payload)) ?? { changed: [], cards: [] }),
    )
    return () => room.unregisterRpcMethod(CARDS_METHOD)
  }, [room])

  const { toggle: toggleMicTrack } = mic
  const { startAudio: resumeAudio } = audio
  // Once Orbit has left as it said it would, hang up instead of reporting a lost agent.
  const stoodBy = leaving && agent.state === 'failed'
  useEffect(() => {
    if (stoodBy) void endSession()
  }, [stoodBy, endSession])
  const failure = active && agent.state === 'failed' && !leaving ? agent.failureReasons.join(' ') : null

  // A conversation started by voice has had no click yet, and the browser reports sound as
  // blocked until the first of it plays. So it counts only once Orbit is there to be heard
  // and it has stayed blocked for a moment.
  const blocked = isConnected && READY.has(agent.state) && !audio.canPlayAudio
  const [stillBlocked, setStillBlocked] = useState(false)
  useEffect(() => {
    if (!blocked) return
    const timer = setTimeout(() => setStillBlocked(true), BLOCKED_AFTER_MS)
    return () => {
      clearTimeout(timer)
      setStillBlocked(false)
    }
  }, [blocked])

  const voice = useMemo<Voice>(
    () => ({
      ...(stoodBy ? standby(false) : display(agent.state, active, wake)),
      active,
      connected: isConnected && READY.has(agent.state),
      wake,
      startedAt,
      error: startError ?? failure,
      start,
      end,
      micEnabled: mic.enabled,
      toggleMic: () => void toggleMicTrack(),
      messages,
      send: (text) => void send(text),
      audioBlocked: blocked && stillBlocked,
      startAudio: () => void resumeAudio(),
      onShow,
      onArrange,
    }),
    [
      agent.state, active, stoodBy, wake, isConnected, startedAt, startError, failure, start, end,
      mic.enabled, toggleMicTrack, messages, send, blocked, stillBlocked, resumeAudio, onShow, onArrange,
    ],
  )

  return <VoiceContext.Provider value={voice}>{children}</VoiceContext.Provider>
}

/** Orbit is about to end the conversation because the user asked it to. */
function isStandby(message: unknown): boolean {
  return typeof message === 'object' && message !== null && (message as { type?: unknown }).type === 'standby'
}

type Show = { type: 'show'; card: CardSpec; push?: Record<string, string> }

/** The message is a request for a card, with at least an id and a kind. */
function isShow(message: unknown): message is Show {
  if (typeof message !== 'object' || message === null) return false
  const { type, card } = message as { type?: unknown; card?: { id?: unknown; kind?: unknown } }
  return type === 'show' && typeof card?.id === 'string' && typeof card.kind === 'string'
}
