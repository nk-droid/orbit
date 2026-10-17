import { useState } from 'react'
import { AttachedFiles, PickFiles } from '../components/Attaching.tsx'
import { Icon } from '../components/Icon.tsx'
import { Empty, Note } from '../components/ui.tsx'
import { addFiles, attachToReply, removeAttached } from '../lib/attachments.ts'
import type { Attached, OnFiles } from '../lib/attachments.ts'
import { discardDraft, draftToGmail, saveDraft, sendDraft, useMail } from '../lib/mail.ts'
import type { Draft, MailDetail } from '../lib/mail.ts'
import { CardFrame } from '../stage/CardFrame.tsx'
import { useStage } from '../stage/stage-context.ts'

/** One email with Orbit's read of it and, if Orbit drafted one, the reply to edit and send. */
export function MailCard({ id, messageId }: { id: string; messageId: string }) {
  const mail = useMail(messageId)
  if (!mail) {
    return (
      <CardFrame id={id} title="Email" icon="mail">
        <Empty icon="mail" title={mail === undefined ? 'Opening' : 'Could not open this email'}>
          {mail === undefined ? 'Fetching the email from Gmail.' : 'It may have been moved or deleted.'}
        </Empty>
      </CardFrame>
    )
  }
  // Keyed so the editor starts from the draft once Orbit has written it.
  return <OpenMail id={id} mail={mail} key={mail.draft ? 'drafted' : 'none'} />
}

/** What the card is busy with, which is said on the button that started it. */
type Busy = 'sending' | 'attaching' | 'working' | null

const said = (e: unknown) => (e instanceof Error ? e.message : 'Something went wrong.')

function OpenMail({ id, mail }: { id: string; mail: MailDetail }) {
  const stage = useStage()
  const [draft, setDraft] = useState(mail.draft)
  const [body, setBody] = useState(mail.draft?.body ?? '')
  // The files on the reply. Kept after it has gone, to show what went with it.
  const [files, setFiles] = useState<Attached[]>(mail.draft?.attachments ?? [])
  const [busy, setBusy] = useState<Busy>(null)
  const [error, setError] = useState<string | null>(null)
  const editable = draft?.status === 'suggested'
  const inGmail = draft?.status === 'in_gmail'
  const gmailLink = inGmail && draft.link?.startsWith('https://mail.google.com/') ? draft.link : null

  const run = async (what: Busy, action: () => Promise<Draft>) => {
    setBusy(what)
    setError(null)
    try {
      setDraft(await action())
    } catch (e) {
      setError(said(e))
    } finally {
      setBusy(null)
    }
  }

  // What the go-ahead is for: the words and the files on screen. The backend sends or
  // saves nothing else, so edits are saved first.
  const asShown = async (go: (id: string, body: string, attachments: string[]) => Promise<Draft>) => {
    if (body !== draft?.body) await saveDraft(mail.id, body)
    return go(mail.id, body, files.map((file) => file.id))
  }

  const attach: OnFiles = async (picked, leftOut) => {
    setBusy('attaching')
    const problem = await addFiles(picked, async (file) => setFiles(await attachToReply(mail.id, file)))
    setError([leftOut, problem].filter(Boolean).join(' ') || null)
    setBusy(null)
  }

  const remove = async (file: Attached) => {
    setBusy('working')
    setError(null)
    try {
      await removeAttached(file.id)
      setFiles((now) => now.filter((kept) => kept.id !== file.id))
    } catch (e) {
      setError(said(e))
    } finally {
      setBusy(null)
    }
  }

  const state =
    draft?.status === 'suggested'
      ? { title: `Reply to ${mail.sender}`, tone: 'warm' as const, tag: 'Awaiting go-ahead' }
      : draft?.status === 'sent'
        ? { title: `Reply to ${mail.sender}`, tone: 'ok' as const, tag: 'Sent' }
        : inGmail
          ? { title: `Reply to ${mail.sender}`, tone: undefined, tag: 'In Gmail drafts' }
          : { title: mail.sender, tone: undefined, tag: draft?.status === 'discarded' ? 'Draft discarded' : undefined }

  return (
    <CardFrame
      id={id}
      icon="mail"
      awaiting={editable}
      pad
      onFiles={editable && !busy ? attach : undefined}
      {...state}
      foot={
        editable ? (
          <>
            <button type="button" className="btn btn--primary" disabled={!!busy || !body.trim()} onClick={() => run('sending', () => asShown(sendDraft))}>
              <Icon name="send" />
              {busy === 'sending' ? 'Sending…' : 'Send it'}
            </button>
            <button type="button" className="btn btn--ghost" disabled={!!busy} onClick={() => run('working', () => discardDraft(mail.id))}>
              Discard
            </button>
            <button
              type="button"
              className="btn btn--ghost win__aside"
              disabled={!!busy || !body.trim()}
              onClick={() => run('working', () => asShown(draftToGmail))}
              title="Puts this reply in your Gmail drafts, with its files, for you to finish there. Nothing is sent."
            >
              Finish in Gmail
            </button>
          </>
        ) : gmailLink ? (
          <a className="btn btn--sm" href={gmailLink} target="_blank" rel="noopener noreferrer">
            <Icon name="open" />
            Open in Gmail
          </a>
        ) : undefined
      }
    >
      <h2 className="win__subject">{mail.subject}</h2>
      <p className="muted">
        {mail.sender} &lt;{mail.address}&gt;
      </p>
      {mail.gist ? (
        <div style={{ margin: '12px 0' }}>
          <Note title="Orbit · the short version">
            <p>{mail.gist}</p>
          </Note>
        </div>
      ) : (
        mail.needs_reply === null && <p className="muted" style={{ margin: '12px 0' }}>Orbit hasn’t read this one yet.</p>
      )}
      <details className="win__email" open={!draft}>
        <summary>The email</summary>
        <p className="msg__text">{mail.body}</p>
        {mail.body_truncated && <p className="muted">The rest of this email is not shown.</p>}
        {mail.attachments.map((file) =>
          file.view === 'none' ? (
            <span className="attach" key={file.part}>
              <Icon name="file" />
              {file.name}
            </span>
          ) : (
            // A file the app can show opens in a card of its own.
            <button
              type="button"
              className="attach"
              key={file.part}
              onClick={() =>
                stage.show({ id: `file:${mail.id}:${file.part}`, kind: 'file', from: 'mail', message_id: mail.id, part: file.part, title: file.name })
              }
              title="Show it here"
            >
              <Icon name={file.view === 'image' ? 'image' : 'file'} />
              {file.name}
            </button>
          ),
        )}
      </details>

      {draft && (
        <>
          <p className="label" style={{ marginTop: 16 }}>
            Your reply · drafted by Orbit
          </p>
          {editable ? (
            <textarea
              className="draft__body"
              value={body}
              onChange={(e) => setBody(e.target.value)}
              disabled={!!busy}
              aria-label="Reply text"
            />
          ) : (
            <div className="draft__body msg__text">{draft.body}</div>
          )}
          {editable && body.includes('[') && (
            <p className="row__detail">Fill in anything in [square brackets] before sending: those are details Orbit didn’t have.</p>
          )}
          {/* Once the reply has gone the backend no longer keeps its files, so they only name what went. */}
          <AttachedFiles files={files} openable={editable} onRemove={editable && !busy ? remove : undefined} />
          {editable && (
            <PickFiles className="btn btn--sm btn--ghost draft__attach" onPick={attach} disabled={!!busy}>
              <Icon name="clip" />
              {busy === 'attaching' ? 'Attaching…' : 'Attach a file'}
            </PickFiles>
          )}
        </>
      )}
      {!draft && mail.needs_reply === false && <p className="muted">Orbit read this as something that doesn’t need an answer from you.</p>}
      {editable && (
        <p className="muted" style={{ marginTop: 8, fontSize: 12 }}>
          Sending goes out through Gmail as a reply in this thread. Drop a file on this card to attach it.
        </p>
      )}
      {inGmail && (
        <p className="muted" style={{ marginTop: 8, fontSize: 12 }}>
          This reply is in your Gmail drafts for you to finish there. Nothing was sent.
        </p>
      )}
      {error && <p className="row__detail is-error">{error}</p>}
    </CardFrame>
  )
}
