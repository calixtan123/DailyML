import { type FormEvent, useEffect, useRef, useState } from 'react'
import type { SavedSubmission } from './PracticeFeedback'
import type { LocalRequest } from './api'

type Section = { id: string; title: string }
type HistoryEntry = {
  item_id: string
  local_date: string
  section_id: string
  section_title: string
  title: string
  status: string
  completed_date: string | null
}
type HistoryPage = { entries: HistoryEntry[]; next_cursor: string | null }
type Source = { id: string; title: string; url: string }
type Rubric = { id: string; description: string; weight: number }
type HistoryDetail = {
  item_id: string
  local_date: string
  question: {
    id: string
    version: number
    title: string
    prompt: string
    objective: string
    section_title: string
    concept_id: string
    criteria: Rubric[]
    sources: Source[]
    reference_explanation: string
    hint?: string | null
  }
  draft: { text: string; version: number }
  submission: (SavedSubmission & { disputes?: Dispute[] }) | null
  support: { reveals: Array<'hint' | 'explanation' | 'gap_explanation'>; gap_completed: boolean } | null
  assessed_at: number | null
  review: { due_date: string | null; awaiting_assessment: boolean } | null
}
type Dispute = {
  id: string
  status: 'open' | 'resolved'
  target_request_id: string
  role: 'original' | 'revision'
  objection: string
  selected_request_id: string | null
  assessments: GradingAssessment[]
}
type GradingAssessment = Exclude<SavedSubmission['request'], { id: null }>
type LoadState<T> =
  | { kind: 'loading' }
  | { kind: 'error' }
  | { kind: 'ready'; value: T }

const STATUS_LABELS: Record<string, string> = {
  not_started: 'Not started',
  unfinished: 'Unfinished draft',
  pending_assessment: 'Awaiting assessment',
  ready_to_send: 'Grading ready to send',
  awaiting_access: 'Waiting for grading access',
  awaiting_allowance: 'Waiting for plan allowance',
  queued: 'Queued for grading',
  in_flight: 'Grading in progress',
  needs_clarification: 'Clarification requested',
  outcome_unknown: 'Grading outcome unknown',
  failed_recoverable: 'Grading needs a retry',
  assessed: 'Assessed',
  support_viewed: 'Support viewed',
  self_reported_gap: 'Self-reported gap',
}

function requestPath(filters: { section: string; start: string; end: string }, cursor?: string, day?: string) {
  const parameters = new URLSearchParams()
  if (filters.section) parameters.set('section_id', filters.section)
  if (filters.start) parameters.set('start_date', filters.start)
  if (filters.end) parameters.set('end_date', filters.end)
  if (day) parameters.set('activity_date', day)
  if (cursor) parameters.set('cursor', cursor)
  const query = parameters.toString()
  return query ? `/api/history?${query}` : '/api/history'
}

function Feedback({ submission }: { submission: SavedSubmission }) {
  const tracks = [
    { label: 'Original assessment', answer: submission, assisted: Boolean(submission.assisted) },
    ...(submission.revision ? [{ label: 'Assisted revision assessment', answer: submission.revision, assisted: submission.revision.supported }] : []),
  ]
  return (
    <section aria-label="Saved assessments">
      <h3>Saved answers and feedback</h3>
      {tracks.map(({ label, answer, assisted }) => (
        <details key={label} open>
          <summary>{label}</summary>
          <p>{assisted ? 'Assisted answer — learning support was used before submission.' : 'Independent answer — no learning support was used before submission.'}</p>
          <p className="practice-original">{answer.assessed_text}</p>
          {answer.request.feedback?.status === 'assessed' && (
            <>
              <p>Grade {answer.request.grade} of 3</p>
              <p>{answer.request.feedback.summary}</p>
              {answer.request.feedback.corrections.length > 0 && (
                <ul>{answer.request.feedback.corrections.map((correction, index) => <li key={index}>{correction}</li>)}</ul>
              )}
              <details>
                <summary>Teaching detail</summary>
                <p>{answer.request.feedback.model_answer}</p>
                <ol>{answer.request.feedback.reasoning.map((step, index) => <li key={index}>{step}</li>)}</ol>
                <p>{answer.request.feedback.example}</p>
              </details>
            </>
          )}
          {answer.request.feedback?.status === 'needs_clarification' && <p>{answer.request.feedback.explanation}</p>}
          {!answer.request.feedback && (
            <p>{answer.request.message ?? STATUS_LABELS[answer.request.state] ?? 'Feedback is not available yet.'}</p>
          )}
          {answer.request.state === 'awaiting_access' && <a href="#/settings">Open grading settings</a>}
        </details>
      ))}
      {submission.versions.length > 1 && (
        <details>
          <summary>Answer versions</summary>
          {submission.versions.map((version) => (
            <div key={version.id}>
              <h4>{version.kind === 'revision' ? 'Assisted revision' : version.kind === 'clarification' ? 'Clarification' : 'Original answer'}</h4>
              <p className="practice-original">{version.text}</p>
              <HistoricalVersionEvidence request={version.request} />
            </div>
          ))}
        </details>
      )}
    </section>
  )
}

function HistoricalVersionEvidence({ request }: { request: SavedSubmission['versions'][number]['request'] }) {
  const historical = request as typeof request & { finished_at?: number | null }
  return (
    <section aria-label="Saved version assessment">
      {historical.finished_at !== undefined && historical.finished_at !== null && (
        <p>Assessment saved <time dateTime={new Date(historical.finished_at * 1000).toISOString()}>{new Date(historical.finished_at * 1000).toLocaleString()}</time></p>
      )}
      {request.feedback?.status === 'assessed' && (
        <>
          <p>Grade {request.grade} of 3</p>
          <p>{request.feedback.summary}</p>
          <details>
            <summary>Saved version explanation</summary>
            <p>{request.feedback.model_answer}</p>
            <ol>{request.feedback.reasoning.map((step, index) => <li key={index}>{step}</li>)}</ol>
            <p>{request.feedback.example}</p>
          </details>
        </>
      )}
      {request.feedback?.status === 'needs_clarification' && <p>{request.feedback.explanation}</p>}
      {!request.feedback && request.message && <p>{request.message}</p>}
    </section>
  )
}

function AssessmentComparison({ assessment }: { assessment: GradingAssessment }) {
  return (
    <section className="practice-attempt" aria-label="Saved assessment comparison">
      <p>Assessment grade {assessment.grade ?? 'pending'} ({assessment.state})</p>
      {assessment.feedback?.status === 'assessed' && (
        <>
          <p>{assessment.feedback.summary}</p>
          {assessment.feedback.corrections.length > 0 && <ul>{assessment.feedback.corrections.map((correction, index) => <li key={index}>{correction}</li>)}</ul>}
          <details>
            <summary>Saved assessment explanation</summary>
            <p>{assessment.feedback.model_answer}</p>
            <ol>{assessment.feedback.reasoning.map((step, index) => <li key={index}>{step}</li>)}</ol>
            <p>{assessment.feedback.example}</p>
          </details>
        </>
      )}
      {assessment.feedback?.status === 'needs_clarification' && <p>{assessment.feedback.explanation}</p>}
      {!assessment.feedback && assessment.message && <p>{assessment.message}</p>}
    </section>
  )
}

function Detail({ detail, onResume }: { detail: HistoryDetail; onResume: (itemId: string) => void }) {
  const disputes = detail.submission?.disputes ?? []
  const canInspectMarking = detail.submission !== null
    || detail.support?.reveals.includes('explanation')
    || detail.support?.reveals.includes('gap_explanation')
  return (
    <article className="panel" aria-labelledby="history-detail-title">
      <h2 id="history-detail-title">{detail.question.title}</h2>
      <p>{detail.question.prompt}</p>
      <p>{detail.question.objective}</p>
      <p>Saved on <time dateTime={detail.local_date}>{detail.local_date}</time></p>
      {detail.submission && <p>Completed on <time dateTime={detail.submission.completed_date}>{detail.submission.completed_date}</time></p>}
      {detail.assessed_at !== null && <p>Assessment saved {new Date(detail.assessed_at * 1000).toLocaleString()}</p>}
      {detail.review?.awaiting_assessment && detail.submission?.request.state === 'assessed'
        && <p role="status">Awaiting accepted assessment</p>}
      {detail.review?.due_date && <p>Current review date <time dateTime={detail.review.due_date}>{detail.review.due_date}</time></p>}
      {detail.draft.text && (
        <details open={!detail.submission}>
          <summary>Saved draft</summary>
          <p className="practice-original">{detail.draft.text}</p>
        </details>
      )}
      {detail.question.hint && detail.support?.reveals.includes('hint') && (
        <section className="practice-support-content" aria-label="Saved hint">
          <h3>Hint</h3>
          <p>{detail.question.hint}</p>
        </section>
      )}
      {detail.support && (
        <details>
          <summary>Support used</summary>
          {detail.support.reveals.length > 0 && <p>Viewed support: {detail.support.reveals.join(', ')}</p>}
          {detail.support.gap_completed && <p>A self-reported knowledge gap was saved.</p>}
        </details>
      )}
      {detail.submission && <Feedback submission={detail.submission} />}
      {disputes.map(dispute => (
        <section key={dispute.id} aria-label="Saved assessment dispute">
          <h3>Assessment dispute</h3>
          <p>{dispute.status === 'resolved' ? 'Resolved' : 'Open'} {dispute.role} assessment dispute</p>
          <p>{dispute.objection}</p>
          {dispute.selected_request_id && <p>Selected assessment: {dispute.selected_request_id === dispute.target_request_id ? 'Original assessment' : 'Reassessment'}.</p>}
          {dispute.assessments.map(assessment => <AssessmentComparison key={assessment.id} assessment={assessment} />)}
        </section>
      ))}
      {canInspectMarking ? (
        <details>
          <summary>Saved question and marking guide</summary>
          <p>Question version {detail.question.version}</p>
          <p>{detail.question.reference_explanation}</p>
          <ul>{detail.question.criteria.map(criterion => <li key={criterion.id}>{criterion.description}</li>)}</ul>
          <ul>{detail.question.sources.map(source => <li key={source.id}><a href={source.url} target="_blank" rel="noreferrer">{source.title}</a></li>)}</ul>
        </details>
      ) : <p>Resume practice to open the saved marking guide and learning support.</p>}
      <button type="button" onClick={() => onResume(detail.item_id)}>
        {detail.submission?.request.can_send || detail.submission?.revision?.request.can_send
          ? 'Recover saved answer' : 'Resume practice'}
      </button>
    </article>
  )
}

/** Browse local learning records and open exact saved evidence or resumable work. */
export function History({ request, onResume, day }: {
  request: LocalRequest
  onResume: (itemId: string) => void
  day?: string
}) {
  const [sections, setSections] = useState<LoadState<Section[]>>({ kind: 'loading' })
  const [filters, setFilters] = useState({ section: '', start: '', end: '' })
  const [applied, setApplied] = useState(filters)
  const [page, setPage] = useState<LoadState<HistoryPage>>({ kind: 'loading' })
  const [nextCursor, setNextCursor] = useState<string | undefined>()
  const [loadingMore, setLoadingMore] = useState(false)
  const [loadMoreError, setLoadMoreError] = useState(false)
  const [pageRetry, setPageRetry] = useState(0)
  const [selectedItemId, setSelectedItemId] = useState<string>()
  const [detail, setDetail] = useState<LoadState<HistoryDetail> | undefined>()
  const requestSequence = useRef(0)
  const itemId = selectedItemId ?? (day && page.kind === 'ready' ? page.value.entries[0]?.item_id : undefined)

  useEffect(() => {
    if (day) return
    const controller = new AbortController()
    request<Section[]>('/api/history/sections', { signal: controller.signal }).then(
      value => { if (!controller.signal.aborted) setSections({ kind: 'ready', value }) },
      () => { if (!controller.signal.aborted) setSections({ kind: 'error' }) },
    )
    return () => controller.abort()
  }, [request, day])

  useEffect(() => {
    const controller = new AbortController()
    const sequence = ++requestSequence.current
    const requestingMore = nextCursor !== undefined
    if (requestingMore) {
      setLoadingMore(true)
      setLoadMoreError(false)
    } else {
      setPage({ kind: 'loading' })
      setLoadingMore(false)
      setLoadMoreError(false)
    }
    request<HistoryPage>(requestPath(applied, nextCursor, day), { signal: controller.signal }).then(
      value => {
        if (controller.signal.aborted || sequence !== requestSequence.current) return
        setPage(previous => previous.kind === 'ready' && requestingMore
          ? { kind: 'ready', value: { entries: [...previous.value.entries, ...value.entries], next_cursor: value.next_cursor } }
          : { kind: 'ready', value })
        setLoadingMore(false)
      },
      () => {
        if (controller.signal.aborted || sequence !== requestSequence.current) return
        if (requestingMore) {
          setLoadingMore(false)
          setLoadMoreError(true)
        } else {
          setPage({ kind: 'error' })
        }
      },
    )
    return () => controller.abort()
  }, [request, applied, nextCursor, pageRetry, day])

  function applyFilters(event: FormEvent) {
    event.preventDefault()
    setSelectedItemId(undefined)
    setDetail(undefined)
    setNextCursor(undefined)
    setApplied({ ...filters })
  }

  function loadMore() {
    if (page.kind === 'ready' && page.value.next_cursor) setNextCursor(page.value.next_cursor)
  }

  function retryPageRequest() {
    setPageRetry(previous => previous + 1)
  }

  useEffect(() => {
    if (!itemId) return
    const controller = new AbortController()
    setDetail({ kind: 'loading' })
    request<HistoryDetail>(`/api/history/${itemId}`, { signal: controller.signal }).then(
      value => { if (!controller.signal.aborted) setDetail({ kind: 'ready', value }) },
      () => { if (!controller.signal.aborted) setDetail({ kind: 'error' }) },
    )
    return () => controller.abort()
  }, [request, itemId])

  return (
    <div className="history">
      {day ? <h2>Saved practice</h2> : <h1>History</h1>}
      {!day && <form className="panel" onSubmit={applyFilters}>
        <div>
          <label htmlFor="history-section">Section</label>
          <select id="history-section" value={filters.section} onChange={event => setFilters({ ...filters, section: event.target.value })}>
            <option value="">All sections</option>
            {sections.kind === 'ready' && sections.value.map(section => <option key={section.id} value={section.id}>{section.title}</option>)}
          </select>
        </div>
        {sections.kind === 'error' && <p role="alert">Could not load sections. You can still search all saved history.</p>}
        <div>
          <label htmlFor="history-start">From date</label>
          <input id="history-start" type="date" value={filters.start} onChange={event => setFilters({ ...filters, start: event.target.value })} />
        </div>
        <div>
          <label htmlFor="history-end">To date</label>
          <input id="history-end" type="date" value={filters.end} onChange={event => setFilters({ ...filters, end: event.target.value })} />
        </div>
        <button type="submit">Apply filters</button>
      </form>}
      {page.kind === 'loading' && <p role="status">Loading saved history…</p>}
      {page.kind === 'error' && (day
        ? <p role="alert">Could not load saved practice. <button type="button" onClick={retryPageRequest}>Retry</button></p>
        : <p role="alert">Could not load saved history. Change a filter or try again.</p>)}
      {loadingMore && <p role="status">Loading more saved history…</p>}
      {loadMoreError && <p role="alert">Could not load more saved history. <button type="button" onClick={retryPageRequest}>Retry loading more</button></p>}
      {page.kind === 'ready' && (
        page.value.entries.length === 0 ? <p className="panel" role="status">
          {day ? 'No saved practice for this day.' : 'No saved learning matches these filters.'}
        </p> : (
          <section aria-label="Saved learning history">
            <ul>
              {page.value.entries.map(entry => (
                <li key={entry.item_id}>
                  <button type="button" aria-pressed={entry.item_id === itemId}
                    onClick={() => setSelectedItemId(entry.item_id)}>{entry.title}</button>
                  <p>{entry.section_title} · <time dateTime={entry.local_date}>{entry.local_date}</time> · {STATUS_LABELS[entry.status] ?? entry.status}</p>
                </li>
              ))}
            </ul>
            {page.value.next_cursor && <button type="button" onClick={loadMore} disabled={loadingMore}>Load more history</button>}
          </section>
        )
      )}
      {detail?.kind === 'loading' && <p role="status">Loading saved learning evidence…</p>}
      {detail?.kind === 'error' && <p role="alert">Could not load this saved learning record.</p>}
      {detail?.kind === 'ready' && <Detail detail={detail.value} onResume={onResume} />}
    </div>
  )
}
