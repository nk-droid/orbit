import { useEffect, useRef } from 'react'
import { Empty } from '../components/ui.tsx'
import { clockTime } from '../lib/time.ts'
import { CardFrame } from '../stage/CardFrame.tsx'
import { speakerOf } from '../voice/speaker.ts'
import { useVoice } from '../voice/voice-context.ts'

/** Everything said in this conversation, oldest first. */
export function TranscriptCard({ id }: { id: string }) {
  const voice = useVoice()
  const end = useRef<HTMLDivElement>(null)
  const { messages } = voice

  // Keep the newest turn in view as transcripts stream in.
  useEffect(() => {
    end.current?.scrollIntoView({ block: 'nearest' })
  }, [messages])

  return (
    <CardFrame id={id} title="Conversation" icon="wave" tag={messages.length > 0 ? String(messages.length) : undefined} pad>
      {messages.length === 0 ? (
        <Empty icon="wave" title="Nothing said yet">
          {voice.active ? 'What you and Orbit say appears here.' : 'Start talking and the conversation appears here.'}
        </Empty>
      ) : (
        <div className="win__turns">
          {messages.map((message) => {
            const orbit = speakerOf(message) === 'orbit'
            return (
              <article key={message.id} className={`turn${orbit ? ' turn--orbit' : ''}`}>
                <header>
                  <span className="label">{orbit ? 'Orbit' : 'You'}</span>
                  <time>{clockTime(new Date(message.timestamp))}</time>
                </header>
                <p>{message.message}</p>
              </article>
            )
          })}
          <div ref={end} />
        </div>
      )}
    </CardFrame>
  )
}
