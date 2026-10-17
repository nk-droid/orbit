import { Icon } from '../components/Icon.tsx'
import { useGoogleStatus } from '../lib/integrations.ts'
import { linkRows } from '../lib/links.ts'
import { CardFrame } from '../stage/CardFrame.tsx'

/** What Orbit is connected to, and whether each connection answers. */
export function ConnectionsCard({ id }: { id: string }) {
  const google = useGoogleStatus()
  return (
    <CardFrame id={id} title="Connections" icon="sliders">
      {google?.account && <p className="label" style={{ padding: '12px 16px 4px' }}>{google.account}</p>}
      <ul className="links" style={{ padding: '6px 0' }}>
        {linkRows(google).map((link) => (
          <li key={link.name}>
            <Icon name={link.icon} />
            {link.name}
            <span className={link.tone === 'ok' ? undefined : `is-${link.tone}`} title={link.detail}>
              {link.status}
            </span>
          </li>
        ))}
      </ul>
    </CardFrame>
  )
}
