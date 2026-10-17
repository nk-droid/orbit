import { useState } from 'react'
import type { FormEvent } from 'react'
import { Icon } from '../components/Icon.tsx'
import { Orb } from '../components/Orb.tsx'
import type { OrbState } from '../voice/state.ts'
import { useVoice } from '../voice/voice-context.ts'

/** The command bar: type to Orbit, mute the microphone, or start and end the conversation. */
export function Dock({ orb, label }: { orb: OrbState; label: string }) {
  const voice = useVoice()
  const [text, setText] = useState('')

  const submit = (e: FormEvent) => {
    e.preventDefault()
    const message = text.trim()
    if (!message || !voice.connected) return
    voice.send(message)
    setText('')
  }

  return (
    <footer className="dock">
      <Orb
        small
        size="46px"
        state={orb}
        onClick={voice.active ? voice.end : voice.start}
        title={voice.active ? 'End the conversation' : 'Talk to Orbit'}
      />
      <div className="dock__state">
        <b>{label}</b>
        <span className={voice.error ? 'is-error' : undefined}>{voice.error ?? voice.hint}</span>
      </div>
      <form className="dock__input" onSubmit={submit}>
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={
            voice.connected
              ? 'Type to Orbit, or just say it…'
              : voice.active
                ? 'Orbit is joining…'
                : voice.wake
                  ? 'Say “Orbit” or press the orb to start talking'
                  : 'Press the orb to start talking to Orbit'
          }
          disabled={!voice.connected}
          aria-label="Message Orbit"
        />
        {voice.active ? (
          <button
            type="button"
            className={`dock__mic${voice.micEnabled ? '' : ' is-muted'}`}
            onClick={voice.toggleMic}
            title={voice.micEnabled ? 'Mute microphone' : 'Unmute microphone'}
            aria-label={voice.micEnabled ? 'Mute microphone' : 'Unmute microphone'}
          >
            <Icon name={voice.micEnabled ? 'mic' : 'micOff'} />
          </button>
        ) : (
          <button type="button" className="dock__mic" onClick={voice.start} title="Talk to Orbit" aria-label="Talk to Orbit">
            <Icon name="mic" />
          </button>
        )}
      </form>
      {voice.active && (
        <button type="button" className="btn btn--ghost" onClick={voice.end}>
          <Icon name="stop" />
          End
        </button>
      )}
    </footer>
  )
}
