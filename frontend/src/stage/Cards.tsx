import { lazy, Suspense } from 'react'
import { ApprovalCard } from '../cards/ApprovalCard.tsx'
import { ConnectionsCard } from '../cards/ConnectionsCard.tsx'
import { FileCard } from '../cards/FileCard.tsx'
import { BrowserCard, ListCard } from '../cards/FoundCards.tsx'
import { MailCard } from '../cards/MailCard.tsx'
import { NeedsCard } from '../cards/NeedsCard.tsx'
import { NoteCard } from '../cards/NoteCard.tsx'
import { RemindersCard } from '../cards/RemindersCard.tsx'
import { ScreenshotCard } from '../cards/ScreenshotCard.tsx'
import { SheetCard } from '../cards/SheetCard.tsx'
import { TodayCard } from '../cards/TodayCard.tsx'
import { TranscriptCard } from '../cards/TranscriptCard.tsx'
import { TrayCard } from '../cards/TrayCard.tsx'
import { UploadCard } from '../cards/UploadCard.tsx'
import type { CardSpec } from './stage-context.ts'
import { useStage } from './stage-context.ts'

// Charts come in a part of the app of their own, fetched the first time one is drawn.
const ChartCard = lazy(() => import('../cards/ChartCard.tsx'))

function Card({ spec }: { spec: CardSpec }) {
  switch (spec.kind) {
    case 'needs':
      return <NeedsCard id={spec.id} />
    case 'today':
      return <TodayCard id={spec.id} />
    case 'reminders':
      return <RemindersCard id={spec.id} />
    case 'connections':
      return <ConnectionsCard id={spec.id} />
    case 'transcript':
      return <TranscriptCard id={spec.id} />
    case 'mail':
      return <MailCard id={spec.id} messageId={spec.message_id} />
    case 'approval':
      return <ApprovalCard id={spec.id} actionId={spec.action_id} />
    case 'list':
      return <ListCard card={spec} />
    case 'file':
      return <FileCard card={spec} />
    case 'sheet':
      return <SheetCard card={spec} />
    case 'screenshot':
      return <ScreenshotCard card={spec} />
    case 'note':
      return <NoteCard card={spec} />
    case 'tray':
      return <TrayCard id={spec.id} />
    case 'upload':
      return <UploadCard card={spec} />
    case 'browser':
      return <BrowserCard card={spec} />
    case 'chart':
      return (
        <Suspense fallback={null}>
          <ChartCard card={spec} />
        </Suspense>
      )
    default:
      // A kind this version of the app does not know: show nothing, not a broken card.
      return null
  }
}

/** Every open card. Each stays mounted in the background, so its shelf chip stays current. */
export function Cards() {
  const { cards } = useStage()
  return cards.map((card) => <Card spec={card.spec} key={card.spec.id} />)
}
