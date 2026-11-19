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
  assessments: Array<{ id: string; state: string; grade: number | null }>
}
type LoadState<T> =
  | { kind: 'loading' }
  | { kind: 'error' }
  | { kind: 'ready'; value: T }

const STATUS_LABELS: Record<string, string> = {
  not_started: 'Not started',
  unfinished: 'Unfinished draft',
  pending_assessment: 'Awaiting assessment',
  assessed: 'Assessed',
}

function requestPath(filters: { section: string; start: string; end: string }, cursor?: string) {
  const parameters = new URLSearchParams()
  if (filters.section) parameters.set('section_id', filters.section)
  if (filters.start) parameters.set('start_date', filters.start)
  if (filters.end) parameters.set('end_date', filters.end)
  if (cursor) parameters.set('cursor', cursor)
  const query = parameters.toString()
  return query ? `/api/history?${query}` : '/api/history'
}

function Feedback({ submission }: { submission: SavedSubmission }) {
  const tracks = [
    { label: 'Original assessment', answer: submission },
    ...(submission.revision ? [{ label: 'Assisted revision assessment', answer: submission.revision }] : []),
  ]
  return (
    <section aria-label="Saved assessments">
      <h3>Saved answers and feedback</h3>
      {tracks.map(({ label, answer }) => (
        <details key={label} open>
          <summary>{label}</summary>
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
          {!answer.request.feedback && <p>{answer.request.message ?? 'Feedback is not available yet.'}</p>}
        </details>
      ))}
      {submission.versions.length > 1 && (
        <details>
          <summary>Answer versions</summary>
          {submission.versions.map((version) => (
            <div key={version.id}>
              <h4>{version.kind === 'revision' ? 'Assisted revision' : version.kind === 'clarification' ? 'Clarification' : 'Original answer'}</h4>
              <p className="practice-original">{version.text}</p>
            </div>
          ))}
        </details>
      )}
    </section>
  )
}

function Detail({ detail, onResume }: { detail: HistoryDetail; onResume: (itemId: string) => void }) {
  const disputes = detail.submission?.disputes ?? []
  return (
    <article className="panel" aria-labelledby="history-detail-title">
      <h2 id="history-detail-title">{detail.question.title}</h2>
      <p>{detail.question.prompt}</p>
      <p>{detail.question.objective}</p>
      <p>Saved on <time dateTime={detail.local_date}>{detail.local_date}</time></p>
      {detail.submission && <p>Completed on <time dateTime={detail.submission.completed_date}>{detail.submission.completed_date}</time></p>}
      {detail.assessed_at !== null && <p>Assessment saved {new Date(detail.assessed_at * 1000).toLocaleString()}</p>}
      {detail.review?.awaiting_assessment && <p role="status">Awaiting accepted assessment</p>}
      {detail.review?.due_date && <p>Current review date <time dateTime={detail.review.due_date}>{detail.review.due_date}</time></p>}
      {detail.draft.text && <details><summary>Saved draft</summary><p className="practice-original">{detail.draft.text}</p></details>}
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
          {dispute.assessments.map(assessment => <p key={assessment.id}>Assessment grade {assessment.grade ?? 'pending'} ({assessment.state})</p>)}
        </section>
      ))}
      <details>
        <summary>Saved question and marking guide</summary>
        <p>Question version {detail.question.version}</p>
        <p>{detail.question.reference_explanation}</p>
        <ul>{detail.question.criteria.map(criterion => <li key={criterion.id}>{criterion.description}</li>)}</ul>
        <ul>{detail.question.sources.map(source => <li key={source.id}><a href={source.url} target="_blank" rel="noreferrer">{source.title}</a></li>)}</ul>
      </details>
      <button type="button" onClick={() => onResume(detail.item_id)}>
        {detail.submission?.request.can_send ? 'Recover saved answer' : 'Resume practice'}
      </button>
    </article>
  )
}

/** Browse local learning records and open exact saved evidence or resumable work. */
export function History({ request, onResume }: { request: LocalRequest; onResume: (itemId: string) => void }) {
  const [sections, setSections] = useState<LoadState<Section[]>>({ kind: 'loading' })
  const [filters, setFilters] = useState({ section: '', start: '', end: '' })
  const [applied, setApplied] = useState(filters)
  const [page, setPage] = useState<LoadState<HistoryPage>>({ kind: 'loading' })
  const [nextCursor, setNextCursor] = useState<string | undefined>()
  const [detail, setDetail] = useState<LoadState<HistoryDetail> | undefined>()
  const requestSequence = useRef(0)

  useEffect(() => {
    const controller = new AbortController()
    request<Section[]>('/api/history/sections', { signal: controller.signal }).then(
      value => { if (!controller.signal.aborted) setSections({ kind: 'ready', value }) },
      () => { if (!controller.signal.aborted) setSections({ kind: 'error' }) },
    )
    return () => controller.abort()
  }, [request])

  useEffect(() => {
    const controller = new AbortController()
    const sequence = ++requestSequence.current
    setPage({ kind: 'loading' })
    request<HistoryPage>(requestPath(applied, nextCursor), { signal: controller.signal }).then(
      value => {
        if (controller.signal.aborted || sequence !== requestSequence.current) return
        setPage(previous => previous.kind === 'ready' && nextCursor
          ? { kind: 'ready', value: { entries: [...previous.value.entries, ...value.entries], next_cursor: value.next_cursor } }
          : { kind: 'ready', value })
      },
      () => { if (!controller.signal.aborted && sequence === requestSequence.current) setPage({ kind: 'error' }) },
    )
    return () => controller.abort()
  }, [request, applied, nextCursor])

  function applyFilters(event: FormEvent) {
    event.preventDefault()
    setDetail(undefined)
    setNextCursor(undefined)
    setApplied(filters)
  }

  function loadMore() {
    if (page.kind === 'ready' && page.value.next_cursor) setNextCursor(page.value.next_cursor)
  }

  async function openDetail(itemId: string) {
    setDetail({ kind: 'loading' })
    try {
      setDetail({ kind: 'ready', value: await request<HistoryDetail>(`/api/history/${itemId}`) })
    } catch {
      setDetail({ kind: 'error' })
    }
  }

  return (
    <main className="content history" id="main-content">
      <h1>History</h1>
      <form className="panel" onSubmit={applyFilters}>
        <label htmlFor="history-section">Section</label>
        <select id="history-section" value={filters.section} onChange={event => setFilters({ ...filters, section: event.target.value })}>
          <option value="">All sections</option>
          {sections.kind === 'ready' && sections.value.map(section => <option key={section.id} value={section.id}>{section.title}</option>)}
        </select>
        {sections.kind === 'error' && <p role="alert">Could not load sections. You can still search all saved history.</p>}
        <label htmlFor="history-start">From date</label>
        <input id="history-start" type="date" value={filters.start} onChange={event => setFilters({ ...filters, start: event.target.value })} />
        <label htmlFor="history-end">To date</label>
        <input id="history-end" type="date" value={filters.end} onChange={event => setFilters({ ...filters, end: event.target.value })} />
        <button type="submit">Apply filters</button>
      </form>
      {page.kind === 'loading' && <p role="status">Loading saved history…</p>}
      {page.kind === 'error' && <p role="alert">Could not load saved history. Change a filter or try again.</p>}
      {page.kind === 'ready' && (
        page.value.entries.length === 0 ? <p className="panel" role="status">No saved learning matches these filters.</p> : (
          <section aria-label="Saved learning history">
            <ul>
              {page.value.entries.map(entry => (
                <li key={entry.item_id}>
                  <button type="button" onClick={() => void openDetail(entry.item_id)}>{entry.title}</button>
                  <p>{entry.section_title} · <time dateTime={entry.local_date}>{entry.local_date}</time> · {STATUS_LABELS[entry.status] ?? entry.status}</p>
                </li>
              ))}
            </ul>
            {page.value.next_cursor && <button type="button" onClick={loadMore}>Load more history</button>}
          </section>
        )
      )}
      {detail?.kind === 'loading' && <p role="status">Loading saved learning evidence…</p>}
      {detail?.kind === 'error' && <p role="alert">Could not load this saved learning record.</p>}
      {detail?.kind === 'ready' && <Detail detail={detail.value} onResume={onResume} />}
    </main>
  )
}
