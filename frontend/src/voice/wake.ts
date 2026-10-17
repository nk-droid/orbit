/**
 * Waking Orbit by voice: in standby the app listens for its name, and starts a conversation
 * when it is said.
 *
 * The listening is done by the browser's own speech recognition, running on this device
 * (`processLocally`), so nothing said near the computer in standby leaves it. A browser that
 * cannot recognise speech on the device gets no wake word: this never falls back to a
 * recognition service on the network.
 * See https://developer.mozilla.org/docs/Web/API/SpeechRecognition/processLocally
 */

import { HEAR_ONLY_THE_ROOM } from './microphone.ts'

/** Set VITE_WAKE_WORD=off to keep the microphone closed in standby. */
export const WAKE_WORD = import.meta.env.VITE_WAKE_WORD !== 'off'

const LANG = 'en-US'
const RESTART_MS = 1000
// The recogniser's first guess at a word is often revised a moment later ("Or bit" becomes
// "Or bitter"), so the name has to stay heard for this long, or the utterance has to be over.
const SETTLE_MS = 600
// A recogniser that keeps ending this soon after it starts is not going to work.
const SHORT_RUN_MS = 5000
const SHORT_RUNS_ALLOWED = 5

// What people say before the name. Whatever follows has to be the name itself.
const LEAD_IN = /^(?:(?:hey|hi|hello|ok|okay|wake up)\s+)*/
const NAME = /^orbit\b/

/**
 * Whether something heard in standby is a call to Orbit: its name at the start, as in
 * "Orbit", "Hey Orbit, what's on today?" or "Wake up, Orbit". The name in the middle of a
 * sentence, as in "the moon is in orbit", is not.
 */
export function isWake(heard: string): boolean {
  const words = heard
    .toLowerCase()
    .replace(/[^a-z]+/g, ' ')
    .trim()
  return NAME.test(words.replace(LEAD_IN, ''))
}

// TypeScript's DOM typings have the recogniser's events but not the recogniser itself, and
// nothing yet for recognising on the device or from a microphone track.
type Recogniser = {
  lang: string
  continuous: boolean
  interimResults: boolean
  processLocally: boolean
  onresult: ((event: SpeechRecognitionEvent) => void) | null
  onerror: ((event: SpeechRecognitionErrorEvent) => void) | null
  onend: (() => void) | null
  start(track?: MediaStreamTrack): void
  abort(): void
}
type OnDevice = { langs: string[]; processLocally: true }
type RecogniserClass = {
  new (): Recogniser
  available?(options: OnDevice): Promise<'available' | 'downloadable' | 'downloading' | 'unavailable'>
  install?(options: OnDevice): Promise<boolean>
}
type Browser = {
  SpeechRecognition?: RecogniserClass
  webkitSpeechRecognition?: RecogniserClass
}

const browser = globalThis as unknown as Browser

// With these there is no point trying again: the microphone or the recogniser is not to be had.
const FATAL = new Set<SpeechRecognitionErrorCode>([
  'not-allowed',
  'service-not-allowed',
  'language-not-supported',
  'audio-capture',
])

/** Whether speech can be recognised on this device, fetching the browser's speech pack if that is all that is missing. */
async function onDevice(Recogniser: RecogniserClass): Promise<boolean> {
  if (!Recogniser.available || !Recogniser.install) return false
  const options: OnDevice = { langs: [LANG], processLocally: true }
  const state = await Recogniser.available(options)
  if (state === 'available') return true
  if (state === 'unavailable') return false
  return Recogniser.install(options)
}

/**
 * Listens on the microphone until Orbit's name is heard, then calls `onWake` once and lets
 * the microphone go. `onListening` is told when listening starts and stops. Returns how to
 * stop early. If this browser cannot do it, nothing happens.
 */
export function listenForWake(onWake: () => void, onListening: (listening: boolean) => void): () => void {
  let stopped = false
  let recogniser: Recogniser | null = null
  let track: MediaStreamTrack | null = null
  let restart: ReturnType<typeof setTimeout> | undefined
  let settling: ReturnType<typeof setTimeout> | undefined
  let shortRuns = 0

  const stop = () => {
    if (stopped) return
    stopped = true
    clearTimeout(restart)
    clearTimeout(settling)
    recogniser?.abort()
    track?.stop()
    onListening(false)
  }

  const listen = (Recogniser: RecogniserClass, microphone: MediaStreamTrack) => {
    if (microphone.readyState !== 'live') return stop()
    const startedAt = Date.now()
    const r = (recogniser = new Recogniser())
    r.lang = LANG
    r.continuous = true
    r.interimResults = true
    r.processLocally = true
    // The recogniser is deliberately not given the name to expect (its `phrases`): told to
    // expect "Orbit", it guesses "Hey Orbit" for whatever is said, and Orbit wakes at anything.
    const woken = () => {
      stop()
      onWake()
    }
    r.onresult = (event) => {
      if (stopped) return
      // Results arrive word by word as guesses, then once more when the utterance is over.
      let called = false
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const result = event.results[i]
        if (!isWake(result[0].transcript)) continue
        if (result.isFinal) return woken()
        called = true
      }
      if (!called) {
        clearTimeout(settling)
        settling = undefined
      } else if (settling === undefined) {
        settling = setTimeout(woken, SETTLE_MS)
      }
    }
    r.onerror = (event) => {
      if (FATAL.has(event.error)) stop()
    }
    // The recogniser ends by itself now and then. Standby goes on, so it is started again.
    r.onend = () => {
      if (stopped) return
      clearTimeout(settling)
      settling = undefined
      shortRuns = Date.now() - startedAt < SHORT_RUN_MS ? shortRuns + 1 : 0
      if (shortRuns >= SHORT_RUNS_ALLOWED) return stop()
      restart = setTimeout(() => listen(Recogniser, microphone), RESTART_MS)
    }
    try {
      r.start(microphone)
    } catch {
      stop()
    }
  }

  const begin = async () => {
    const Recogniser = browser.SpeechRecognition ?? browser.webkitSpeechRecognition
    if (!Recogniser || !(await onDevice(Recogniser)) || stopped) return
    const stream = await navigator.mediaDevices.getUserMedia({ audio: HEAR_ONLY_THE_ROOM })
    track = stream.getAudioTracks()[0]
    if (stopped) return track.stop()
    onListening(true)
    listen(Recogniser, track)
  }
  // No wake word is not an error worth showing: the orb still starts a conversation.
  begin().catch(stop)

  return stop
}
