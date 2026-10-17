import { PickFiles } from '../components/Attaching.tsx'
import { Icon } from '../components/Icon.tsx'
import { useTrayState } from '../lib/attachments.ts'
import { CardFrame } from '../stage/CardFrame.tsx'
import type { CardSpec } from '../stage/stage-context.ts'
import { useStage } from '../stage/stage-context.ts'
import { useHandOver } from '../stage/useHandOver.ts'
import { useVoice } from '../voice/voice-context.ts'

/**
 * Orbit asking for a file that is on the user's computer. Orbit opens this when it needs
 * one: a browser lets a page open the computer's file picker only on a press, so the press
 * is here. The file is picked or dropped, Orbit is told, and the card goes.
 */
export function UploadCard({ card }: { card: Extract<CardSpec, { kind: 'upload' }> }) {
  const voice = useVoice()
  const stage = useStage()
  const handOver = useHandOver()
  const { adding, problem } = useTrayState()

  const notNow = () => {
    stage.close(card.id)
    if (voice.connected) voice.send('Never mind, I will not add a file now.')
  }

  return (
    <CardFrame
      id={card.id}
      title="Upload"
      icon="clip"
      tone="warm"
      tag="From your computer"
      pad
      onFiles={adding ? undefined : handOver}
      foot={
        <button type="button" className="btn btn--ghost" onClick={notNow}>
          Not now
        </button>
      }
    >
      <p className="label">Orbit is asking for</p>
      <h2 className="win__subject">{card.asking || 'A file from your computer'}</h2>
      <div className="upload__drop">
        <PickFiles className="btn btn--primary" onPick={handOver} disabled={adding}>
          <Icon name="clip" />
          {adding ? 'Adding…' : 'Choose a file'}
        </PickFiles>
        <p className="muted">or drop it here</p>
      </div>
      {problem && <p className="row__detail is-error">{problem}</p>}
      <p className="muted upload__note">
        The file stays on this computer until an email carries it, and nothing is sent before you have heard the draft and agreed.
      </p>
    </CardFrame>
  )
}
