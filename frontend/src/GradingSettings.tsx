import { useEffect, useRef, useState } from 'react'
import type { LocalRequest } from './api'

type Catalog = {
  models: {slug: string; display_name: string}[]
  selected: string | null
  version: number
  overridden: boolean
  source: string
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
export function GradingSettings({ request, connected, uncertainAction, onUncertainActionChange }: {
  request: LocalRequest
  connected: boolean
  uncertainAction: GradingAction | undefined
  onUncertainActionChange: (action: GradingAction | undefined) => void
}) {
  const [catalog, setCatalog] = useState<Catalog>()
  const [reference, setReference] = useState<Reference>()
  const [job, setJob] = useState<Job | null>(null)
  const [count, setCount] = useState<number>()
  const [error, setError] = useState('')
  const [catalogError, setCatalogError] = useState('')
  const [revision, setRevision] = useState(0)
  const [busy, setBusy] = useState(false)
  const action = useRef<AbortController | undefined>(undefined)

  const report = (failure: unknown) => setError(failure instanceof Error ? failure.message : 'Could not load the connection test.')
  const pending = PENDING.has(job?.state ?? '')
  const selected = catalog?.models.find(model => model.slug === catalog.selected)?.slug ?? ''
  const canRunTest = connected && !!reference && !busy && !pending && !!(uncertainAction || selected)
  const visibleError = error || catalogError

  useEffect(() => () => action.current?.abort(), [])

  useEffect(() => {
    const controller = new AbortController()
    const options = { signal: controller.signal }
    setError('')
    setCatalogError('')
    setReference(undefined)
    setCatalog(undefined)
    void request<{reference: Reference; latest: Job | null}>(TEST, options).then(reply => {
      if (!controller.signal.aborted) { setReference(reply.reference); setJob(reply.latest) }
    }).catch(failure => { if (!controller.signal.aborted) report(failure) })
    if (connected) {
      void request<Catalog>('/api/connection/models', options).then(reply => {
        if (!controller.signal.aborted) setCatalog(reply)
      }).catch(failure => {
        if (!controller.signal.aborted) {
          setCatalogError(failure instanceof Error ? failure.message : 'Could not load grading models.')
        }
      })
    }
    return () => controller.abort()
  }, [request, connected, revision])

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

  async function selectModel(slug: string) {
    if (!catalog || !slug || busy) return
    setBusy(true)
    setError('')
    const controller = new AbortController()
    action.current = controller
    try {
      const selection = await request<Omit<Catalog, 'models'>>('/api/connection/model', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model_id: slug, version: catalog.version }), signal: controller.signal,
      })
      if (!controller.signal.aborted) setCatalog({ ...catalog, ...selection })
    } catch (failure) { if (!controller.signal.aborted) report(failure) }
    finally { if (!controller.signal.aborted) setBusy(false) }
  }

  const retry = job?.error && job.model_id === selected ? job : null
  const preview = retry?.reference ?? (pending ? job?.reference : reference)
  async function runTest() {
    if (!canRunTest) return
    setBusy(true)
    setError('')
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
    } catch (failure) { if (!controller.signal.aborted) report(failure) }
    finally { if (!controller.signal.aborted) setBusy(false) }
  }

  return <section className="panel grading" aria-labelledby="grading-heading">
    <h2 id="grading-heading">Grading connection test</h2>
    <p className="muted">This test sends the fixed question, reference answer, marking guide and sources to OpenAI using your ChatGPT plan or credits.</p>
    {preview && <dl className="reference">
      <dt>Reference question</dt><dd>{preview.question}</dd>
      <dt>Reference answer</dt><dd>{preview.answer}</dd>
    </dl>}
    {!connected && <p>Connect ChatGPT to choose a model and run the test.</p>}
    {connected && (catalog || uncertainAction) && <div className="settings-row">
      {catalog && <div><label htmlFor="grading-model">Grading model</label>
        {catalog.overridden && <p className="muted">Set by {catalog.source} configuration.</p>}
      </div>}
      <div className="actions">
        {catalog && <select id="grading-model" value={selected} disabled={busy || pending || catalog.overridden || !!uncertainAction}
          onChange={event => void selectModel(event.target.value)}>
          <option value="" disabled>{catalog.models.length ? 'Choose a model' : 'No models available'}</option>
          {catalog.models.map(model => <option key={model.slug} value={model.slug}>{model.display_name}</option>)}
        </select>}
        <button className="primary" onClick={() => void runTest()} disabled={!canRunTest}>
          {uncertainAction ? 'Check previous test' : retry?.error === 'schema_unsupported' ? 'Retry in JSON mode' :
            retry ? 'Retry connection test' : 'Run connection test'}
        </button>
      </div>
    </div>}
    {job && <Result job={job} />}
    <p className="muted" aria-live="polite">Model attempts in the last 24 hours: {count ?? 'unavailable'}</p>
    {visibleError && <div><p role="alert" className="error">{visibleError}</p>
      <button disabled={busy} onClick={() => setRevision(r => r + 1)}>Reload connection test</button></div>}
  </section>
}
