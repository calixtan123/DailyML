import { ChevronDown } from 'lucide-react'
import { type FormEvent, useEffect, useRef, useState } from 'react'
import { AnswerText, Assessment, GradeScore } from './Assessment'
import { formatPracticeDate } from './practiceDates'
import { submissionProgress, type SavedSubmission } from './PracticeFeedback'
import type { LocalRequest } from './api'

type Section = { id: string; title: string }
type HistoryEntry = {
  item_id: string
  local_date: string
  section_id: string
  section_title: string
  title: string
  status: string
  grade: number | null
  has_open_dispute: boolean
  has_revision_draft: boolean
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

function SavedAssessment({ request, title }: { request: SavedSubmission['request']; title?: string }) {
  if (request.feedback?.status === 'assessed') {
    return <Assessment grade={request.grade} feedback={request.feedback} sources={request.reference?.sources} title={title} />
  }
  if (request.feedback?.status === 'needs_clarification') return <p>{request.feedback.explanation}</p>
  return <p>{request.message ?? STATUS_LABELS[request.state] ?? 'Feedback is not available yet.'}</p>
}

function Feedback({ submission }: { submission: SavedSubmission }) {
  const tracks = [
    { label: 'Original attempt', answer: submission, assisted: Boolean(submission.assisted) },
    ...(submission.revision ? [{ label: 'Assisted revision', answer: submission.revision, assisted: submission.revision.supported }] : []),
  ]
  return (
    <section className="saved-feedback" aria-label="Saved assessments">
      {tracks.map(({ label, answer, assisted }) => (
        <section className="practice-attempt" key={label} aria-label={label}>
          <h4>{label}</h4>
          <SavedAssessment request={answer.request} />
          <AnswerText label={answer.request.state === 'self_reported_gap' ? 'Recorded answer' : 'Your submitted answer'}
            text={answer.assessed_text} open />
          {answer.request.state !== 'self_reported_gap' && <p className="muted">{assisted
            ? 'Assisted answer — learning support was used before submission.'
            : 'Independent answer — no learning support was used before submission.'}</p>}
          {answer.request.state === 'awaiting_access' && <a href="#/settings">Open grading settings</a>}
        </section>
      ))}
      {submission.versions.length > 1 && (
        <details>
          <summary>Answer versions</summary>
          {submission.versions.map(version => (
            <section className="practice-attempt" key={version.id} aria-label="Saved version assessment">
              <h4>{version.kind === 'revision' ? 'Assisted revision' : version.kind === 'clarification' ? 'Clarification' : 'Original answer'}</h4>
              <p className="practice-original">{version.text}</p>
              {version.request.finished_at != null && (
                <p>Assessment saved <time dateTime={new Date(version.request.finished_at * 1000).toISOString()}>{new Date(version.request.finished_at * 1000).toLocaleString()}</time></p>
              )}
              <SavedAssessment request={version.request} />
            </section>
          ))}
        </details>
      )}
    </section>
  )
}

function entrySummary(detail: HistoryDetail): Pick<HistoryEntry, 'status' | 'grade' | 'has_open_dispute' | 'has_revision_draft' | 'completed_date'> {
  if (!detail.submission) {
    return {
      status: /[^\t\n\v\f\r ]/.test(detail.draft.text) ? 'unfinished' : 'support_viewed',
      grade: null,
      has_open_dispute: false,
      has_revision_draft: false,
      completed_date: null,
    }
  }
  const submission = detail.submission
  const { grading_status, grade, has_open_dispute } = submissionProgress(submission)
  return {
    status: grading_status,
    grade,
    has_open_dispute,
    has_revision_draft: (submission.revision_draft?.version ?? 0) > 0,
    completed_date: submission.completed_date,
  }
}

function Detail({ detail, onResume }: {
  detail: HistoryDetail
  onResume: (itemId: string) => void
}) {
  const disputes = detail.submission?.disputes ?? []
  const canInspectMarking = detail.submission !== null
    || detail.support?.reveals.includes('explanation')
    || detail.support?.reveals.includes('gap_explanation')
  return (
    <article className="history-detail" aria-labelledby={`history-question-${detail.item_id}`}>
      <header className="history-question">
        <h3 id={`history-question-${detail.item_id}`}>{detail.question.title}</h3>
        <p>{detail.question.prompt}</p>
        <p className="muted">{detail.question.objective}</p>
        {detail.review?.due_date && <p className="muted">Current review date <time dateTime={detail.review.due_date}>{formatPracticeDate(detail.review.due_date)}</time></p>}
        {detail.review?.awaiting_assessment && detail.submission?.request.state === 'assessed'
          && <p role="status">Awaiting accepted assessment</p>}
      </header>
      {detail.submission && <Feedback submission={detail.submission} />}
      {detail.draft.text && <AnswerText label="Saved draft" text={detail.draft.text} open={!detail.submission} />}
      <details>
        <summary>Practice dates</summary>
        <p>Saved on <time dateTime={detail.local_date}>{detail.local_date}</time></p>
        {detail.submission && <p>Completed on <time dateTime={detail.submission.completed_date}>{detail.submission.completed_date}</time></p>}
        {detail.assessed_at !== null && <p>Assessment saved {new Date(detail.assessed_at * 1000).toLocaleString()}</p>}
      </details>
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
      {disputes.map(dispute => (
        <section key={dispute.id} aria-label="Saved assessment dispute">
          <h4>Assessment dispute</h4>
          <p>{dispute.status === 'resolved' ? 'Resolved' : 'Open'} {dispute.role} assessment dispute</p>
          <p>{dispute.objection}</p>
          {dispute.selected_request_id && <p>Selected assessment: {dispute.selected_request_id === dispute.target_request_id ? 'Original assessment' : 'Reassessment'}.</p>}
          {dispute.assessments.map(assessment => {
            const label = assessment.id === dispute.target_request_id ? 'Disputed assessment' : 'Reassessment'
            return <div className="practice-attempt" key={assessment.id}>
              <SavedAssessment request={assessment} title={assessment.id === dispute.selected_request_id ? `Accepted ${label.toLowerCase()}` : label} />
            </div>
          })}
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
  const [detail, setDetail] = useState<{ itemId: string; revision: number; state: Exclude<LoadState<HistoryDetail>, { kind: 'loading' }> }>()
  const [detailRevision, setDetailRevision] = useState(0)
  const requestSequence = useRef(0)

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

  function refreshDetail() {
    setDetailRevision(previous => previous + 1)
  }

  useEffect(() => {
    const itemId = selectedItemId
    if (!itemId) return
    const controller = new AbortController()
    const settle = (state: Exclude<LoadState<HistoryDetail>, { kind: 'loading' }>) => {
      if (controller.signal.aborted) return
      setDetail({ itemId, revision: detailRevision, state })
      if (state.kind === 'ready') {
        const summary = entrySummary(state.value)
        setPage(previous => previous.kind === 'ready'
          ? { kind: 'ready', value: {
            ...previous.value,
            entries: previous.value.entries.map(entry => entry.item_id === itemId ? { ...entry, ...summary } : entry),
          } } : previous)
      }
    }
    request<HistoryDetail>(`/api/history/${itemId}`, { signal: controller.signal }).then(
      value => settle(value.item_id === itemId ? { kind: 'ready', value } : { kind: 'error' }),
      () => settle({ kind: 'error' }),
    )
    return () => controller.abort()
  }, [request, selectedItemId, detailRevision])

  return (
    <div className="history">
      {day ? <h2>Saved practice</h2> : <h1 tabIndex={-1}>History</h1>}
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
              {page.value.entries.map(entry => {
                const open = entry.item_id === selectedItemId
                const evidence = detail?.itemId === entry.item_id && detail.revision === detailRevision ? detail.state : undefined
                const titleId = `history-title-${entry.item_id}`
                const metaId = `history-meta-${entry.item_id}`
                const statusId = `history-status-${entry.item_id}`
                const detailId = `history-detail-${entry.item_id}`
                return (
                  <li className="history-record" key={entry.item_id}>
                    <button className="history-record-toggle" type="button" aria-expanded={open} aria-controls={detailId}
                      aria-labelledby={titleId} aria-describedby={`${metaId} ${statusId}`}
                      onClick={() => {
                        setDetail(undefined)
                        setSelectedItemId(open ? undefined : entry.item_id)
                      }}>
                      <span className="history-record-name">
                        <strong id={titleId}>{entry.title}</strong>
                        <span id={metaId}>{entry.section_title} · <time dateTime={entry.local_date}>{entry.local_date}</time></span>
                      </span>
                      <span className="history-record-status" id={statusId}>
                        {entry.has_open_dispute && <span>Dispute open</span>}
                        {entry.has_revision_draft && <span>Revision draft</span>}
                        {entry.grade != null ? <GradeScore grade={entry.grade} />
                          : <span>{STATUS_LABELS[entry.status] ?? entry.status}</span>}
                      </span>
                      <ChevronDown className="history-record-chevron" size={18} aria-hidden="true" />
                    </button>
                    <div id={detailId} className="history-record-body" hidden={!open}>
                      {open && (
                        !evidence ? <p role="status">Loading saved learning evidence…</p>
                          : evidence.kind === 'error' ? <p role="alert">Could not load this saved learning record.{' '}
                            <button type="button" onClick={refreshDetail}>Retry loading record</button>
                          </p>
                            : <Detail detail={evidence.value} onResume={onResume} />
                      )}
                    </div>
                  </li>
                )
              })}
            </ul>
            {page.value.next_cursor && <button type="button" onClick={loadMore} disabled={loadingMore}>Load more history</button>}
          </section>
        )
      )}
    </div>
  )
}
