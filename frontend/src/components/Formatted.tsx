import { createElement, Fragment, useMemo } from 'react'
import type { ReactNode } from 'react'

// The tags the backend's clean HTML is made of (backend/src/writing.py). The backend has
// already taken everything else out. This draws only these all the same, so that nothing
// which slips past it is ever put on the page as HTML.
const DRAWN = new Set([
  ...['p', 'div', 'br', 'hr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'],
  ...['ul', 'ol', 'li', 'dl', 'dt', 'dd', 'blockquote', 'pre'],
  ...['table', 'caption', 'thead', 'tbody', 'tfoot', 'tr', 'th', 'td'],
  ...['a', 'strong', 'b', 'em', 'i', 'u', 's', 'del', 'ins', 'code', 'sub', 'sup', 'mark', 'small'],
])
// Never drawn, and neither is what is inside them.
const DROPPED = new Set([
  ...['script', 'style', 'template', 'noscript', 'iframe', 'object', 'embed', 'svg', 'math'],
  ...['form', 'button', 'select', 'textarea', 'input', 'img', 'audio', 'video', 'canvas'],
  ...['link', 'meta', 'base', 'head', 'title'],
])
// Tags that hold other tags and no words of their own: the gaps between those are not text.
const FRAMES = new Set(['table', 'thead', 'tbody', 'tfoot', 'tr', 'ul', 'ol', 'dl'])
const LEADS_OUT = /^(https?:|mailto:)/i
// How the backend draws a ticked and an unticked box at the start of a list item.
const BOX = /^\s*[☑☐] /

function whole(value: string | null, upper: number): number | undefined {
  const number = Number(value)
  return Number.isInteger(number) && number > 0 && number <= upper ? number : undefined
}

function draw(node: ChildNode, key: number): ReactNode {
  if (node.nodeType === Node.TEXT_NODE) return node.textContent
  if (!(node instanceof Element)) return null
  const tag = node.localName
  if (DROPPED.has(tag)) return null

  const inside = Array.from(node.childNodes)
    .filter((child) => !(FRAMES.has(tag) && child.nodeType === Node.TEXT_NODE && !child.textContent?.trim()))
    .map(draw)
  if (!DRAWN.has(tag)) return <Fragment key={key}>{inside}</Fragment>
  if (tag === 'br' || tag === 'hr') return createElement(tag, { key })

  const props: Record<string, unknown> = { key }
  if (tag === 'a') {
    // A link leads out to the web, in a tab of its own, or it is only its words.
    const href = node.getAttribute('href') ?? ''
    if (!LEADS_OUT.test(href)) return <Fragment key={key}>{inside}</Fragment>
    Object.assign(props, { href, target: '_blank', rel: 'noopener noreferrer' })
  }
  if (tag === 'ol') props.start = whole(node.getAttribute('start'), 1_000_000)
  if (tag === 'td' || tag === 'th') {
    props.colSpan = whole(node.getAttribute('colspan'), 100)
    props.rowSpan = whole(node.getAttribute('rowspan'), 100)
  }
  // An item that begins with a box needs no bullet as well.
  if (tag === 'li' && BOX.test(node.textContent ?? '')) props.className = 'md__box'
  return createElement(tag, props, ...inside)
}

/**
 * Formatted writing, drawn: headings, lists, tables, emphasis, links. It is given as the
 * clean HTML the backend makes from Markdown or HTML, whoever wrote that: Orbit, in a
 * draft or an answer, or somebody else, in a file.
 *
 * With `links="spelled"` each link is followed by where it leads, for a draft the user
 * is asked to agree to.
 */
export function Formatted({ html, links }: { html: string; links?: 'spelled' }) {
  const drawn = useMemo(() => {
    // Parsed into a document of its own, where nothing runs and nothing is fetched.
    const parsed = new DOMParser().parseFromString(html, 'text/html')
    return Array.from(parsed.body.childNodes).map(draw)
  }, [html])
  return <div className={`md${links === 'spelled' ? ' md--spelled' : ''}`}>{drawn}</div>
}
