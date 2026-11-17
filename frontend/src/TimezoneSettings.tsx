import { useEffect, useState, type FormEvent } from 'react'
import type { LocalRequest } from './api'

type Settings = {
  timezone: string
  version: number
  source: 'default' | 'preferences' | 'installation' | 'environment'
}

const SETTINGS = '/api/settings'

export function TimezoneSettings({ request }: { request: LocalRequest }) {
  const [settings, setSettings] = useState<Settings>()
  const [zone, setZone] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [saved, setSaved] = useState(false)
  const [loadVersion, setLoadVersion] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    void request<Settings>(SETTINGS, { signal: controller.signal }).then((result) => {
      if (controller.signal.aborted) return
      setSettings(result)
      setZone(result.timezone)
      setError('')
    }).catch((failure: unknown) => {
      if (!controller.signal.aborted) setError(failure instanceof Error ? failure.message : 'Could not load settings.')
    })
    return () => controller.abort()
  }, [request, loadVersion])

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!settings) return
    setSaving(true)
    setError('')
    setSaved(false)
    try {
      const next = await request<Settings>(SETTINGS, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ timezone: zone.trim(), version: settings.version }),
      })
      setSettings(next)
      setZone(next.timezone)
      setSaved(true)
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : 'Could not save the timezone.')
    } finally {
      setSaving(false)
    }
  }

  const override = settings?.source === 'installation' || settings?.source === 'environment'
  return (
    <section className="panel" aria-labelledby="preferences-heading">
      <h2 id="preferences-heading">Learning preferences</h2>
      {settings && (
        <form className="settings-row timezone-row" onSubmit={(event) => void save(event)}>
          <div>
            <label htmlFor="timezone">Learning timezone</label>
            <p className="muted" id="timezone-help">
              {override
                ? `Set by ${settings.source === 'environment' ? 'the environment (DML_TIMEZONE)' : 'the installation file'}.`
                : 'Use a timezone name such as Europe/London.'}
            </p>
          </div>
          <div className="actions">
            <input id="timezone" value={zone} maxLength={128} readOnly={override}
              aria-describedby="timezone-help" onChange={(event) => {
                setZone(event.target.value)
                setSaved(false)
              }} />
            {!override && <button type="submit" className="primary"
              disabled={saving || !zone.trim() || zone.trim() === settings.timezone}>Save</button>}
          </div>
        </form>
      )}
      {!settings && !error && <p role="status" className="muted">Loading preferences…</p>}
      {error && <div className="settings-row"><p className="error" role="alert">{error}</p>
        <button onClick={() => setLoadVersion((version) => version + 1)}>Reload preferences</button>
      </div>}
      {saved && <p role="status" className="notice">Timezone saved.</p>}
    </section>
  )
}
