import { useEffect, useRef } from 'react'
import { Icon, Logo } from '../components/Icon.tsx'
import { Orb } from '../components/Orb.tsx'
import { Av, Say } from '../components/ui.tsx'
import { useActions } from '../lib/actions.ts'
import { useFileDrop } from '../lib/attachments.ts'
import { USER } from '../lib/profile.ts'
import { clockTime, greeting, shortDate, useNow } from '../lib/time.ts'
import { Cards } from '../stage/Cards.tsx'
import type { CardSpec } from '../stage/stage-context.ts'
import { useStage } from '../stage/stage-context.ts'
import { useHandOver } from '../stage/useHandOver.ts'
import { speakerOf } from '../voice/speaker.ts'
import type { OrbState } from '../voice/state.ts'
import { useVoice } from '../voice/voice-context.ts'
import { Dock } from './Dock.tsx'

// Things to ask for. With a conversation running the words go to Orbit, which pulls up the
// card itself; without one the card is opened straight from the backend.
const TRY: { say: string; card: CardSpec }[] = [
  { say: 'What needs me?', card: { id: 'needs', kind: 'needs' } },
  { say: 'What’s on today?', card: { id: 'today', kind: 'today' } },
  { say: 'What reminders do I have?', card: { id: 'reminders', kind: 'reminders' } },
]

/** The whole app: Orbit in the middle, and whatever it has pulled up around it. */
export function Home() {
  const voice = useVoice()
  const stage = useStage()
  const { layout, setLayer, setShelf } = stage
  const now = useNow(15_000)

  // Amber while a card that needs a go-ahead is in front of the user and Orbit is not mid-sentence.
  const resting = voice.orb === 'standby' || voice.orb === 'listening'
  const orb: OrbState = stage.awaiting && resting ? 'approval' : voice.orb
  const label = orb === 'approval' ? 'Awaiting go-ahead' : voice.label

  const heard = voice.messages.findLast((m) => speakerOf(m) === 'you')?.message
  const said = voice.messages.findLast((m) => speakerOf(m) === 'orbit')?.message
  const shown = stage.cards.filter((card) => !card.back).length
  const hidden = stage.cards.length - shown
  // With one card at a time Orbit keeps a place of its own, and lies on its side where that
  // place is a strip: above a card, or alone on a screen that is short but wide.
  const sideways = layout === 'stacked' ? shown > 0 : layout === 'beside' && shown === 0

  const ask = ({ say, card }: (typeof TRY)[number]) => (voice.connected ? voice.send(say) : stage.show(card))

  // A file dropped anywhere on the app is for Orbit: the one it asked for, or one to wait
  // until it is asked to attach it. A card that takes files itself, such as a reply, keeps
  // what is dropped on it.
  const drop = useFileDrop(useHandOver())

  return (
    <div className="app" data-state={orb} data-layout={layout} {...drop.handlers}>
      <div className="main">
        <header className="top">
          <span className="top__logo">
            <Logo />
          </span>
          <div className="crumb">Orbit</div>
          <div className="readouts">
            <span className="readouts__voice">
              <i className="dot" />
              Voice <b className={voice.error ? 'is-error' : undefined}>{label}</b>
            </span>
            <span className="readouts__date">
              <b>{shortDate(now)}</b>
            </span>
            <span>
              <b>{clockTime(now)}</b>
            </span>
          </div>
          <button
            type="button"
            className="top__link"
            onClick={() => stage.show({ id: 'transcript', kind: 'transcript' })}
            title="Conversation"
            aria-label="Show the conversation"
          >
            <Icon name="wave" />
          </button>
          <button
            type="button"
            className="top__link"
            onClick={() => stage.show({ id: 'connections', kind: 'connections' })}
            title="Connections"
            aria-label="Show connections"
          >
            <Icon name="sliders" />
          </button>
          <span title={USER.name}>
            <Av you>{USER.initials}</Av>
          </span>
        </header>

        <main className={`stage stage--home${shown > 0 ? ' has-card' : ''}`} ref={setLayer}>
          <section className={`home${sideways ? ' home--row' : ''}`}>
            <div className="home__in">
              <h1 className="home__hello">
                {greeting(now)}, {USER.name}.
              </h1>
              <div className="home__orb">
                <Orb
                  state={orb}
                  onClick={voice.active ? voice.end : voice.start}
                  title={voice.active ? 'End the conversation' : 'Talk to Orbit'}
                />
              </div>
              <div className="home__words">
                <p className={`home__state${voice.error ? ' is-error' : ''}`}>{label}</p>
                <p className="home__heard">{heard && `You: “${heard}”`}</p>
                <p className={`home__line${said ? ' is-orbit' : ''}${voice.error ? ' is-error' : ''}`}>
                  {voice.error ?? said ?? (voice.active ? voice.hint : `${wakeOrPress(voice.wake)} and tell me what you need.`)}
                </p>
                {voice.audioBlocked && (
                  <button type="button" className="btn btn--primary" onClick={voice.startAudio}>
                    Turn on Orbit’s voice
                  </button>
                )}
                <div className="chips home__try">
                  {TRY.map((item) => (
                    <button type="button" className="chip" key={item.say} onClick={() => ask(item)}>
                      {item.say}
                    </button>
                  ))}
                </div>
              </div>
            </div>
          </section>

          <div className="wins">
            <Cards />
          </div>

          {/* Shown one at a time, the shelf is only there when it holds something. */}
          <aside className="shelf" hidden={layout === 'windows' ? stage.cards.length === 0 : hidden === 0}>
            <span className="label shelf__label" title="In the background">
              <Icon name="layers" />
              <span>
                <span className="shelf__word">In background · </span>
                {hidden}
              </span>
            </span>
            <div className="shelf__items" ref={setShelf} />
            {layout === 'windows' && shown > 0 && hidden === 0 && (
              <span className="shelf__tip">
                Drag a card to move it · <kbd>Esc</kbd> sends the front card to the background
              </span>
            )}
            {shown > 1 && (
              <button type="button" className="btn btn--sm shelf__clear" onClick={stage.clear}>
                Send all to background <Say>clear the stage</Say>
              </button>
            )}
          </aside>
        </main>

        <Dock orb={orb} label={label} />
      </div>
      <DraftWatcher />
      {drop.over && (
        <div className="dropzone">
          <Icon name="clip" />
          <b>Drop to add</b>
          <p>Orbit keeps the file ready to attach to an email.</p>
        </div>
      )}
    </div>
  )
}

/** How to start a conversation from standby. */
function wakeOrPress(wake: boolean): string {
  return wake ? 'Say “Orbit” or press the orb' : 'Press the orb'
}

/** Pulls up a card for every draft Orbit makes in this conversation, as soon as it exists. */
function DraftWatcher() {
  const voice = useVoice()
  const { show } = useStage()
  const actions = useActions(voice.active)
  const opened = useRef(new Set<string>())

  useEffect(() => {
    // Only this conversation's drafts; the log itself keeps everything.
    const since = voice.startedAt ? voice.startedAt / 1000 - 5 : Infinity
    for (const action of actions ?? []) {
      if (action.status !== 'awaiting_approval' || action.created_at < since || opened.current.has(action.id)) continue
      opened.current.add(action.id)
      show({ id: `action:${action.id}`, kind: 'approval', action_id: action.id })
    }
  }, [actions, voice.startedAt, show])

  return null
}
