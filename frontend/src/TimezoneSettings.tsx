import { useEffect, useRef, useState, type FormEvent } from 'react'
import { LocalApiError, type LocalRequest } from './api'
import { preferenceSource, type Preferences } from './preferences'

const SETTINGS = '/api/settings'

/** Manage the learning timezone and explain which configuration layer controls each setting. */
export function TimezoneSettings({ request, latestPreferences, onPreferencesChange }: {
  request: LocalRequest
  latestPreferences?: Preferences
  onPreferencesChange?: (settings: Preferences) => void
}) {
  const [loaded, setLoaded] = useState<Preferences>()
  const [draft, setDraft] = useState<{ zone: string; version: number }>()
  const [saving, setSaving] = useState(false)
  const [loading, setLoading] = useState(true)
  const [conflict, setConflict] = useState(false)
  const [error, setError] = useState('')
  const [saved, setSaved] = useState(false)
  const [loadVersion, setLoadVersion] = useState(0)
  const zoneInput = useRef<HTMLInputElement>(null)
  const reloadButton = useRef<HTMLButtonElement>(null)
  const errorText = useRef<HTMLParagraphElement>(null)
  const focusZoneOnLoad = useRef(false)
  const reloadPending = useRef(false)

  const settings = latestPreferences && (!loaded || latestPreferences.version > loaded.version)
    ? latestPreferences : loaded
  const zone = draft?.zone ?? settings?.timezone ?? ''
  const override = settings?.source === 'installation' || settings?.source === 'environment'
  const canSave = !!settings && !loading && !saving && !override && !conflict && !!zone.trim()
    && zone.trim() !== settings.timezone

  useEffect(() => {
    if (loading) return
    if (error && (loaded || loadVersion > 0)) errorText.current?.focus()
    if (!error && focusZoneOnLoad.current) zoneInput.current?.focus()
    focusZoneOnLoad.current = false
  }, [error, loaded, loading, loadVersion])

  useEffect(() => {
    const controller = new AbortController()
    void request<Preferences>(SETTINGS, { signal: controller.signal }).then(result => {
      if (controller.signal.aborted) return
      focusZoneOnLoad.current = document.activeElement === reloadButton.current
      setLoaded(result)
      onPreferencesChange?.(result)
      if (reloadPending.current) {
        reloadPending.current = false
        setDraft(previous => previous && { ...previous, version: result.version })
      }
      setConflict(false)
      setError('')
    }).catch((failure: unknown) => {
      if (controller.signal.aborted) return
      reloadPending.current = false
      setError(failure instanceof Error ? failure.message : 'Could not load settings.')
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false)
    })
    return () => controller.abort()
  }, [request, loadVersion, onPreferencesChange])

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!settings || !canSave) return
    setSaving(true)
    setError('')
    setSaved(false)
    try {
      const next = await request<Preferences>(SETTINGS, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ timezone: zone.trim(), version: draft?.version ?? settings.version }),
      })
      setLoaded(next)
      onPreferencesChange?.(next)
      setDraft(undefined)
      setSaved(true)
    } catch (failure) {
      if (failure instanceof LocalApiError && failure.status === 409) setConflict(true)
      setError(failure instanceof Error ? failure.message : 'Could not save the timezone.')
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="panel preferences" aria-labelledby="preferences-heading">
      <h2 id="preferences-heading">Learning preferences</h2>
      {settings && (
        <form className="settings-row timezone-row" onSubmit={(event) => void save(event)}>
          <div>
            <label htmlFor="timezone">Learning timezone</label>
            <p className="muted" id="timezone-source">{preferenceSource(settings.source, 'TIMEZONE')}</p>
            {!override && <p className="muted" id="timezone-help">Use a timezone name such as Europe/London.</p>}
          </div>
          <div className="actions">
            <input ref={zoneInput} id="timezone" value={override ? settings.timezone : zone} maxLength={128}
              readOnly={override || loading || saving}
              aria-describedby={override ? 'timezone-source' : 'timezone-source timezone-help'} onChange={event => {
                const next = event.target.value
                setDraft(previous => ({ zone: next, version: previous?.version ?? settings.version }))
                setSaved(false)
              }} />
            {!override && <button type="submit" className="primary" disabled={!canSave}>Save</button>}
          </div>
        </form>
      )}
      {loading && <p role="status" className="muted">Loading preferences…</p>}
      {error && <div className="settings-row">
        <p ref={errorText} className="error" role="alert" tabIndex={-1}>{error}</p>
        <button ref={reloadButton} aria-disabled={loading} onClick={() => {
          if (loading) return
          reloadPending.current = true
          setLoading(true)
          setLoadVersion(version => version + 1)
        }}>Reload preferences</button>
      </div>}
      {saved && <p role="status" className="notice">Timezone saved.</p>}
      {loadVersion > 0 && !loading && !error && settings && draft !== undefined && !override &&
        <p role="status" className="muted">Saved timezone is {settings.timezone}. Review your edit before saving.</p>}
      {settings && <div className="preference-model">
        <h3>Effective grading model</h3>
        <p>{settings.model_id ?? 'No model selected'}</p>
        <p className="muted">{preferenceSource(settings.model_source, 'MODEL_ID')}</p>
      </div>}
      <details className="preference-precedence" open>
        <summary>Configuration order</summary>
        <p className="muted">Each layer takes precedence over the ones above it.</p>
        <ol>
          <li>Packaged defaults</li>
          <li>Saved SQLite preferences</li>
          <li>Installation TOML overrides</li>
          <li>Environment overrides</li>
        </ol>
      </details>
    </section>
  )
}
