import type { ReceivedMessage } from '@livekit/components-react'

export type Speaker = 'you' | 'orbit'

export function speakerOf(message: ReceivedMessage): Speaker {
  if (message.type === 'userTranscript') return 'you'
  if (message.type === 'agentTranscript') return 'orbit'
  // Typed chat: the local participant is the user.
  return message.from?.isLocal ? 'you' : 'orbit'
}
