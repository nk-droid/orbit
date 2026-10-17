import { useEffect, useRef, useState } from 'react'
import { WAKE_WORD, listenForWake } from './wake.ts'

/**
 * Listens for Orbit's name while the app is in standby, and calls `onWake` when it is said.
 * Returns whether it is listening.
 *
 * It only listens once the microphone has been allowed, which happens the first time the
 * user starts a conversation by hand. So the page never asks for the microphone by itself.
 */
export function useWakeWord(standby: boolean, onWake: () => void): boolean {
  const [listening, setListening] = useState(false)
  const allowed = useMicrophoneAllowed()
  const chosen = useChosenTab()
  const wake = useRef(onWake)
  useEffect(() => {
    wake.current = onWake
  }, [onWake])

  useEffect(() => {
    if (!WAKE_WORD || !standby || !allowed || !chosen) return
    return listenForWake(() => wake.current(), setListening)
  }, [standby, allowed, chosen])

  return listening
}

// Held by the one tab that listens for the name.
const TAB_LOCK = 'orbit.wake'

/**
 * Whether this is the tab that listens. With Orbit open in several tabs, every one of them
 * would hear the name and start a conversation of its own, so one tab holds a lock for as
 * long as it is open, and the next takes over when it closes.
 */
function useChosenTab(): boolean {
  const [chosen, setChosen] = useState(false)

  useEffect(() => {
    let release: (() => void) | null = null
    let gone = false
    void navigator.locks?.request(
      TAB_LOCK,
      () =>
        new Promise<void>((resolve) => {
          if (gone) return resolve()
          release = resolve
          setChosen(true)
        }),
    )
    return () => {
      gone = true
      release?.()
      setChosen(false)
    }
  }, [])

  return chosen
}

/** Whether this site may already use the microphone, kept up to date as that changes. */
function useMicrophoneAllowed(): boolean {
  const [allowed, setAllowed] = useState(false)

  useEffect(() => {
    let status: PermissionStatus | null = null
    let gone = false
    const update = () => setAllowed(status?.state === 'granted')
    navigator.permissions?.query({ name: 'microphone' as PermissionName }).then(
      (found) => {
        if (gone) return
        status = found
        status.addEventListener('change', update)
        update()
      },
      // A browser that cannot say is treated as one that has not allowed it.
      () => {},
    )
    return () => {
      gone = true
      status?.removeEventListener('change', update)
    }
  }, [])

  return allowed
}
