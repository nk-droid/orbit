import { Fragment } from 'react'
import { Empty, Subhead } from '../components/ui.tsx'
import type { ProfileField } from '../lib/jobs.ts'
import { useProfile } from '../lib/jobs.ts'
import { CardFrame } from '../stage/CardFrame.tsx'
import type { CardSpec } from '../stage/stage-context.ts'

type ProfileSpec = Extract<CardSpec, { kind: 'profile' }>

function unknown(value: ProfileField['value']): boolean {
  return value === null || value === '' || (Array.isArray(value) && value.length === 0)
}

/**
 * The user's job profile: what every application is filled from, field by field, with where each
 * value came from and what other sources said. Changed by voice, so it has no buttons of its own.
 */
export function ProfileCard({ card }: { card: ProfileSpec }) {
  const profile = useProfile(card.shown_at)
  if (!profile) {
    return (
      <CardFrame id={card.id} title="Job profile" icon="briefcase" pad>
        <Empty icon="briefcase" title={profile === undefined ? 'Loading' : 'Could not show the profile'}>
          {profile === undefined ? 'Fetching your profile.' : 'Ask Orbit to show it again.'}
        </Empty>
      </CardFrame>
    )
  }
  return (
    <CardFrame id={card.id} title="Job profile" icon="briefcase" pad>
      {profile.groups.map((group) => {
        const shown = group.fields.filter((field) => field.needed || !unknown(field.value))
        if (shown.length === 0) return null
        return (
          <section key={group.title} className="profile__group">
            <Subhead>{group.title}</Subhead>
            <dl className="kv kv--wide">
              {shown.map((field) => (
                <Fragment key={field.key}>
                  <dt>{field.label}</dt>
                  <dd>
                    {unknown(field.value) ? (
                      <span className="profile__missing">Not known yet</span>
                    ) : (
                      <span>{Array.isArray(field.value) ? field.value.join(', ') : field.value}</span>
                    )}
                    {field.source && <span className="profile__source">{field.source === 'you' ? 'you said' : field.source}</span>}
                    {field.others.map((other) => (
                      <span key={`${other.source}:${other.value}`} className="profile__other">
                        {other.source} says {other.value}
                      </span>
                    ))}
                  </dd>
                </Fragment>
              ))}
            </dl>
          </section>
        )
      })}
      <section className="profile__group">
        <Subhead>{profile.resumes.length > 1 ? 'Résumés' : 'Résumé'}</Subhead>
        {profile.resumes.length > 0 ? (
          profile.resumes.map((resume) => (
            <p className="profile__line" key={resume.name}>
              {resume.name}
              {resume.role && <span className="profile__source">for {resume.role} roles</span>}
              <span className="profile__source">{resume.source}</span>
            </p>
          ))
        ) : (
          <p className="profile__line">
            <span className="profile__missing">None yet</span>
          </p>
        )}
      </section>
      {profile.answers.length > 0 && (
        <section className="profile__group">
          <Subhead>Kept answers</Subhead>
          <dl className="kv kv--wide">
            {profile.answers.map((kept) => (
              <Fragment key={kept.question}>
                <dt>{kept.question}</dt>
                <dd>{kept.answer}</dd>
              </Fragment>
            ))}
          </dl>
        </section>
      )}
      <section className="profile__group">
        <Subhead>Searches</Subhead>
        <dl className="kv kv--wide">
          {profile.settings.map((setting) => (
            <Fragment key={setting.key}>
              <dt>{setting.label}</dt>
              <dd>{setting.value}</dd>
            </Fragment>
          ))}
        </dl>
      </section>
    </CardFrame>
  )
}
