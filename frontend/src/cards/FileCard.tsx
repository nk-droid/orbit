import type { ReactNode } from 'react'
import { Formatted } from '../components/Formatted.tsx'
import { Icon } from '../components/Icon.tsx'
import { Empty } from '../components/ui.tsx'
import { fileAddress, useFile, useFileBytes } from '../lib/files.ts'
import type { FileInfo } from '../lib/files.ts'
import { CardFrame } from '../stage/CardFrame.tsx'
import type { CardSpec } from '../stage/stage-context.ts'
import { OpenFoot } from './FoundCards.tsx'

type FileSpec = Extract<CardSpec, { kind: 'file' }>

// Plain writing is set as prose. Anything else that is text, such as code or a table of
// figures, keeps its columns.
const PROSE = /\.(txt|text|rst|srt|vtt)$/i

// False only where the browser says it cannot draw a PDF inside a page, as on Android.
const canShowPdf = () => (navigator as { pdfViewerEnabled?: boolean }).pdfViewerEnabled !== false

/**
 * A file shown as itself: a picture, a PDF, text, or a Markdown or HTML file drawn as a
 * page. It comes from Drive or from an email, and the app fetches it from the backend by
 * where it is kept.
 */
export function FileCard({ card }: { card: FileSpec }) {
  const file = useFile(card)
  const drawn = file?.view === 'image' || (file?.view === 'pdf' && canShowPdf())
  const bytes = useFileBytes(card, drawn)

  if (!file) {
    return (
      <CardFrame id={card.id} title={card.title ?? 'File'} icon="file" tag={card.tag || undefined}>
        <Empty icon="file" title={file === undefined ? 'Opening' : 'Could not open this file'}>
          {file === undefined ? 'Fetching the file.' : 'It may have been moved or deleted, or Google is not connected.'}
        </Empty>
      </CardFrame>
    )
  }

  // What is drawn follows what came back, which for a long document is its text, not a PDF.
  const got = bytes && 'url' in bytes ? bytes : undefined
  const problem = bytes && 'problem' in bytes ? bytes.problem : undefined
  const picture = got?.type.startsWith('image/')
  const pdf = got?.type === 'application/pdf'
  const words = got?.text ?? (file.view === 'text' ? file.text : null)
  return (
    <CardFrame
      id={card.id}
      title={file.name}
      icon={file.view === 'image' ? 'image' : 'file'}
      tag={file.kind}
      pad={!pdf}
      tall={pdf}
      foot={foot(card, file)}
    >
      {drawn && !bytes && (
        <Empty icon="file" title="Opening">
          Fetching the file.
        </Empty>
      )}
      {problem && (
        <Empty icon="file" title="Could not show this file">
          {problem}
        </Empty>
      )}
      {got && picture && <img className="file__img" src={got.url} alt={file.name} />}
      {got && picture && file.preview && (
        <p className="file__note">This is a picture of its first page. Open it in Drive for the whole file.</p>
      )}
      {/* The page is fitted to the frame, without the viewer's own bars. */}
      {got && pdf && <iframe className="file__pdf" src={`${got.url}#toolbar=0&navpanes=0&view=FitH`} title={file.name} />}
      {file.view === 'pdf' && !drawn && (
        <Empty icon="file" title="Not shown here">
          This browser cannot show a PDF inside a page. Open it to read it.
        </Empty>
      )}
      {file.html !== null && <Formatted html={file.html} />}
      {file.view === 'html' && <p className="file__note">Shown without its styles, pictures and scripts.</p>}
      {words !== null && (
        <pre className="file__text" data-mono={file.view === 'text' && !PROSE.test(file.name) ? '' : undefined}>
          {words}
        </pre>
      )}
      {got?.text != null && (
        <p className="file__note">This document is too long for Google to show as pages, so this is its text alone.</p>
      )}
      {file.truncated && <p className="file__note">The file is too long to show in full. This is its first part.</p>}
      {file.view === 'none' && (
        <Empty icon="file" title="Not shown here">
          Orbit cannot show this kind of file. {file.link ? 'Open it in Drive to see it.' : ''}
        </Empty>
      )}
    </CardFrame>
  )
}

/** A Drive file opens in Drive. A file from an email has nowhere else, and opens as it is. */
function foot(card: FileSpec, file: FileInfo): ReactNode {
  if (file.link?.startsWith('https://')) return <OpenFoot link={file.link} label="Open in Drive" />
  if (file.view === 'none') return undefined
  return (
    <a className="btn btn--sm" href={fileAddress(card)} target="_blank" rel="noopener noreferrer">
      <Icon name="open" />
      Open
    </a>
  )
}
