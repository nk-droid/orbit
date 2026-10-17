import { addToTray } from '../lib/attachments.ts'
import type { Attached, OnFiles } from '../lib/attachments.ts'
import { useVoice } from '../voice/voice-context.ts'
import { useStage } from './stage-context.ts'

/** What the app says for the user once they have picked the file Orbit asked for. */
function said(added: Attached[]): string {
  const names = added.map((file) => file.name).join(', ')
  return added.length === 1 ? `Attach the file I just added: ${names}.` : `Attach the files I just added: ${names}.`
}

/**
 * Takes files from the user's computer for Orbit. If Orbit asked for one, it is told the
 * moment the file is there, in the user's name: picking the file is their saying so. If
 * nobody asked, the files wait on their card until Orbit is asked to attach them.
 */
export function useHandOver(): OnFiles {
  const voice = useVoice()
  const stage = useStage()
  return async (files, leftOut) => {
    const asked = stage.cards.some((card) => card.spec.kind === 'upload')
    // Shown at once where nobody asked, so that the card says the files are on their way.
    if (!asked) stage.show({ id: 'tray', kind: 'tray' })
    const added = await addToTray(files, leftOut)
    // What could not be added is said on the card that is open.
    if (!asked || added.length === 0) return
    stage.close('upload')
    if (voice.connected) voice.send(said(added))
    // The conversation ended while Orbit was asking: the files wait for the next one.
    else stage.show({ id: 'tray', kind: 'tray' })
  }
}
