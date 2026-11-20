import { useEffect, useRef, useState } from 'react'
import { LocalApiError, type LocalRequest } from './api'
import type { Preferences, PreferenceSource } from './preferences'

type Catalog = {
  models: {slug: string; display_name: string}[]
  selected: string | null
  version: number
  overridden: boolean
  source: PreferenceSource
  settings: Preferences
}
type Reference = { question: string; answer: string; sources: Record<string, string> }
type Feedback = {
  status: 'assessed'; summary: string; corrections: string[]; model_answer: string
  criteria: { criterion_id: string; status: string; justification: string }[]
  reasoning: string[]; example: string; source_ids: string[]; follow_up: string[]
  critical_misconception_ids: string[]
} | { status: 'needs_clarification'; explanation: string }
type Job = { id: string; model_id: string; state: string; grade: number | null; error: string | null; reference: Reference
  message: string | null; feedback: Feedback | null }
/** Request body retained until the server acknowledges a test submission. */
export type GradingAction = { model_id: string; idempotency_key: string; retry_of?: string }
const TEST = '/api/connection/test'
const MODELS = '/api/connection/models'
const PENDING = new Set(['queued', 'preparing', 'in_flight'])
const POLL_MS = 500

function Result({ job }: { job: Job }) {
  const feedback = job.feedback
  if (PENDING.has(job.state)) return <p role="status">Testing the connection with {job.model_id}…</p>
  if (!feedback) return <p role="status" className="notice">{job.message}</p>
  if (feedback.status === 'needs_clarification') {
    return <p role="status">Clarification requested by {job.model_id}. {feedback.explanation}</p>
  }
  return <div className="grading-result">
    <h3>AI-generated feedback</h3>
    <p role="status"><strong>Grade {job.grade} of 3</strong> · {job.model_id}</p>
    <p>{feedback.summary}</p>
    {feedback.corrections.length > 0 && <ul>{feedback.corrections.map((item, i) => <li key={i}>{item}</li>)}</ul>}
    <p><strong>Model answer</strong></p><p>{feedback.model_answer}</p>
    <details><summary>Criteria and teaching detail</summary>
      <ul>{feedback.criteria.map(item => <li key={item.criterion_id}>
        {item.criterion_id} ({item.status.replace('_', ' ')}) — {item.justification}
      </li>)}</ul>
      {feedback.critical_misconception_ids.length > 0 &&
        <p>Critical misconceptions: {feedback.critical_misconception_ids.join(', ')}</p>}
      <ol>{feedback.reasoning.map((step, i) => <li key={i}>{step}</li>)}</ol>
      <p>{feedback.example}</p>
      {feedback.source_ids.map(id => {
        const url = job.reference.sources[id]
        return url?.startsWith('https://') && <p key={id}><a href={url} target="_blank" rel="noreferrer">{id}</a></p>
      })}
      {feedback.follow_up.map((item, i) => <p key={i}>Follow-up: {item}</p>)}
    </details>
  </div>
}

/** Run the grading connection test and display the saved result. */
export function GradingSettings({ request, connected, uncertainAction, onUncertainActionChange,
  latestPreferences, onPreferencesChange }: {
  request: LocalRequest
  connected: boolean
  uncertainAction: GradingAction | undefined
  onUncertainActionChange: (action: GradingAction | undefined) => void
  latestPreferences?: Preferences
  onPreferencesChange?: (settings: Preferences) => void
}) {
  const [catalog, setCatalog] = useState<Catalog>()
  const [reference, setReference] = useState<Reference>()
  const [job, setJob] = useState<Job | null>(null)
  const [count, setCount] = useState<number>()
  const [error, setError] = useState('')
  const [catalogError, setCatalogError] = useState('')
  const [saveError, setSaveError] = useState('')
  const [catalogStale, setCatalogStale] = useState(false)
  const [catalogRevision, setCatalogRevision] = useState(0)
  const [catalogLoading, setCatalogLoading] = useState(false)
  const [draft, setDraft] = useState<{ model: string; version: number }>()
  const [didSave, setDidSave] = useState(false)
  const [revision, setRevision] = useState(0)
  const [busy, setBusy] = useState(false)
  const action = useRef<AbortController | undefined>(undefined)
  const modelErrorText = useRef<HTMLParagraphElement>(null)
  const testErrorText = useRef<HTMLParagraphElement>(null)
  const focusTestError = useRef(false)

  const report = (failure: unknown) => setError(failure instanceof Error ? failure.message : 'Could not load the connection test.')
  const pending = PENDING.has(job?.state ?? '')
  const selected = catalog?.models.find(model => model.slug === catalog.selected)?.slug ?? ''
  const modelUnavailable = catalog?.selected && !selected
  const catalogReady = !!catalog && !catalogLoading && !catalogError && !catalogStale && !saveError
  const canEditModel = connected && catalogReady && !busy && !pending && !catalog.overridden && !uncertainAction
  const canSaveModel = canEditModel && !!draft?.model && draft.model !== selected
  const canRunTest = connected && !!reference && !busy && !pending && !!(uncertainAction || (catalogReady && selected && !canSaveModel))
  const catalogVersion = latestPreferences && catalog && latestPreferences.version > catalog.version
    && latestPreferences.model_id === catalog.selected && latestPreferences.model_source === catalog.source
    ? latestPreferences.version : catalog?.version ?? 0

  useEffect(() => () => action.current?.abort(), [])

  useEffect(() => { if (saveError) modelErrorText.current?.focus() }, [saveError])
  useEffect(() => {
    if ((error || job?.error) && focusTestError.current) {
      focusTestError.current = false
      testErrorText.current?.focus()
    }
    if (job?.state === 'assessed' || job?.state === 'needs_clarification') focusTestError.current = false
  }, [error, job?.state, job?.error])

  useEffect(() => {
    const controller = new AbortController()
    const options = { signal: controller.signal }
    setError('')
    setReference(undefined)
    void request<{reference: Reference; latest: Job | null}>(TEST, options).then(reply => {
      if (!controller.signal.aborted) { setReference(reply.reference); setJob(reply.latest) }
    }).catch(failure => { if (!controller.signal.aborted) report(failure) })
    return () => controller.abort()
  }, [request, revision])

  useEffect(() => {
    if (!connected) return
    const controller = new AbortController()
    setCatalogLoading(true)
    void request<Catalog>(MODELS, { signal: controller.signal }).then(reply => {
      if (controller.signal.aborted) return
      setCatalog(reply)
      if (reply.settings) onPreferencesChange?.(reply.settings)
      setCatalogStale(false)
      setDraft(undefined)
    }).catch(failure => {
      if (!controller.signal.aborted) {
        setCatalogError(failure instanceof Error ? failure.message : 'Could not load grading models.')
      }
    }).finally(() => {
      if (!controller.signal.aborted) setCatalogLoading(false)
    })
    return () => controller.abort()
  }, [request, connected, catalogRevision, onPreferencesChange])

  useEffect(() => {
    const controller = new AbortController()
    void request<{attempts_24h: number}>('/api/usage', { signal: controller.signal }).then(reply => {
      if (!controller.signal.aborted) setCount(reply.attempts_24h)
    }).catch(failure => { if (!controller.signal.aborted) { setCount(undefined); report(failure) } })
    return () => controller.abort()
  }, [request, job?.id, job?.state, revision])

  useEffect(() => {
    if (!job || !pending) return
    const id = job.id
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    async function poll() {
      try {
        const next = await request<Job>('/api/requests/' + id, { signal: controller.signal })
        if (controller.signal.aborted) return
        setJob(next)
        if (PENDING.has(next.state)) timer = setTimeout(poll, POLL_MS)
      } catch (failure) { if (!controller.signal.aborted) report(failure) }
    }
    timer = setTimeout(poll, POLL_MS)
    return () => { clearTimeout(timer); controller.abort() }
  }, [request, job?.id, pending, revision])

  function refreshModels() {
    if (busy || catalogLoading) return
    setCatalogLoading(true)
    setCatalogError('')
    setSaveError('')
    setDidSave(false)
    setCatalogRevision(previous => previous + 1)
  }

  async function saveModel() {
    if (!canSaveModel || !catalog) return
    setBusy(true)
    setSaveError('')
    const controller = new AbortController()
    action.current = controller
    try {
      const selection = await request<Omit<Catalog, 'models'>>('/api/connection/model', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model_id: draft.model, version: draft.version }), signal: controller.signal,
      })
      if (controller.signal.aborted) return
      setCatalog({ ...catalog, ...selection })
      if (selection.settings) onPreferencesChange?.(selection.settings)
      setDraft(undefined)
      setDidSave(true)
    } catch (failure) {
      if (!controller.signal.aborted) {
        setSaveError(failure instanceof Error ? failure.message : 'Could not save the grading model.')
      }
    }
    finally { if (!controller.signal.aborted) setBusy(false) }
  }

  const retry = job?.error && job.model_id === selected ? job : null
  const preview = retry?.reference ?? (pending ? job?.reference : reference)
  async function runTest() {
    if (!canRunTest) return
    setBusy(true)
    setError('')
    focusTestError.current = true
    const controller = new AbortController()
    action.current = controller
    const body = uncertainAction ?? { idempotency_key: crypto.randomUUID(), model_id: selected,
      ...(retry && { retry_of: retry.id }) }
    onUncertainActionChange(body)
    try {
      const next = await request<Job>(TEST, { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body), signal: controller.signal })
      if (!controller.signal.aborted) {
        onUncertainActionChange(undefined)
        setJob(next)
      }
    } catch (failure) {
      if (controller.signal.aborted) return
      if (failure instanceof LocalApiError && failure.status === 409) {
        onUncertainActionChange(undefined)
        setCatalogStale(true)
      }
      report(failure)
    }
    finally { if (!controller.signal.aborted) setBusy(false) }
  }

  return <section className="panel grading" aria-labelledby="grading-heading">
    <h2 id="grading-heading">Grading connection test</h2>
    {connected && <div className="model-settings">
      <div className="settings-row">
        <div>
          <label htmlFor={catalog ? 'grading-model' : undefined}>Grading model</label>
          <p className="muted" id="model-help">The saved model is used for future grading requests.</p>
        </div>
        <button type="button" aria-disabled={catalogLoading || busy} onClick={refreshModels}>Refresh model catalog</button>
      </div>
      {catalogLoading && <p role="status" className="muted">Loading account models…</p>}
      {catalog && <div className="actions model-actions">
        <select id="grading-model" aria-describedby="model-help" value={draft?.model ?? selected}
          disabled={!canEditModel} onChange={event => {
            const model = event.target.value
            setDraft(model === selected ? undefined : { model, version: draft?.version ?? catalogVersion })
            setDidSave(false)
          }}>
          <option value="" disabled>{catalog.models.length ? 'Choose a model' : 'No models available'}</option>
          {catalog.models.map(model => <option key={model.slug} value={model.slug}>{model.display_name}</option>)}
        </select>
        {!catalog.overridden && <button type="button" className="primary" onClick={() => void saveModel()}
          disabled={!canSaveModel}>Save model</button>}
      </div>}
      {(saveError || catalogError || modelUnavailable) && <p ref={modelErrorText} role="alert" className="error" tabIndex={-1}>
        {saveError || catalogError || (catalog?.overridden
          ? `${catalog.selected} is unavailable in this account. Update the configuration override to use an available model.`
          : `${catalog?.selected} is unavailable in this account. Choose and save an available model.`)}
      </p>}
      {didSave && <p role="status" className="notice">Model saved.</p>}
    </div>}
    <p className="muted">This test sends the fixed question, reference answer, marking guide and sources to OpenAI using your ChatGPT plan or credits.</p>
    {preview && <dl className="reference">
      <dt>Reference question</dt><dd>{preview.question}</dd>
      <dt>Reference answer</dt><dd>{preview.answer}</dd>
    </dl>}
    {!connected && <p>Connect ChatGPT to choose a model and run the test.</p>}
    {connected && (catalog || uncertainAction) && <div className="actions">
        <button className="primary" onClick={() => void runTest()} disabled={!canRunTest}>
          {uncertainAction ? 'Check previous test' : retry?.error === 'schema_unsupported' ? 'Retry in JSON mode' :
            retry ? 'Retry connection test' : 'Run connection test'}
        </button>
    </div>}
    {job && !job.error && <Result job={job} />}
    {(error || job?.error) && <div className="settings-row">
      <p ref={testErrorText} role="alert" className="error" tabIndex={-1}>{error || job?.message}</p>
      {error && <button disabled={busy} onClick={() => setRevision(r => r + 1)}>Reload connection test</button>}
    </div>}
    <div className="observed-usage">
      <h3>Observed usage</h3>
      <p aria-live="polite">Model attempts in the last 24 hours: {count ?? 'unavailable'}</p>
      <p className="muted">Counts this app’s sends and uncertain attempts, including practice and connection tests, over a rolling UTC window.</p>
    </div>
  </section>
}
