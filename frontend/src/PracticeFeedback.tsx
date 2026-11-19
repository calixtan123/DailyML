import { type RefObject, useEffect, useRef, useState } from 'react'
import { LocalApiError, type LocalRequest } from './api'
import { ClarificationEditor, type ClarificationEdit } from './ClarificationEditor'
import type { Draft, LeaveEditor } from './drafts'

type Feedback = {
  status: 'assessed'
  summary: string
  corrections: string[]
  model_answer: string
  reasoning: string[]
  example: string
  source_ids: string[]
  follow_up?: string[]
} | { status: 'needs_clarification'; explanation: string }

const REQUEST_LABELS = {
  awaiting_access: 'Waiting for grading access',
  awaiting_allowance: 'Waiting for plan allowance',
  queued: 'Queued for grading',
  in_flight: 'Grading in progress',
  needs_clarification: 'Clarification requested',
  outcome_unknown: 'Grading outcome unknown',
  failed_recoverable: 'Grading needs a retry',
  assessed: 'Grading complete',
}
export type GradingStatus = keyof typeof REQUEST_LABELS | 'ready_to_send'
type GradingRequest = {
  id: string
  state: keyof typeof REQUEST_LABELS
  grade: number | null
  can_send: boolean
  message: string | null
  feedback: Feedback | null
  reference: { sources: Record<string, string> }
}

export type SavedSubmission = {
  text: string
  assessed_text: string
  completed_date: string
  completed_timezone: string
  credited: boolean
  request: GradingRequest
  clarification_draft: Draft | null
}

type FeedbackView = {
  submission?: SavedSubmission
  edit?: ClarificationEdit
  replacement?: SavedSubmission
}

const POLL_MS = 500
const RECOVERABLE = new Set<GradingRequest['state']>([
  'awaiting_access', 'awaiting_allowance', 'outcome_unknown', 'failed_recoverable',
])

function isActive(job: GradingRequest | undefined) {
  return job?.state === 'in_flight' || (job?.state === 'queued' && !job.can_send)
}

function Assessment({ job, feedback }: { job: GradingRequest; feedback: Extract<Feedback, { status: 'assessed' }> }) {
  return (
    <section className="grading-result" aria-label="AI-generated feedback">
      <h3>AI-generated feedback</h3>
      <p role="status"><strong>Grade {job.grade} of 3</strong></p>
      <h4>What you understood</h4>
      <p>{feedback.summary}</p>
      {feedback.corrections.length > 0 && (
        <>
          <h4>What to improve</h4>
          <ul>{feedback.corrections.map((point, index) => <li key={index}>{point}</li>)}</ul>
        </>
      )}
      <h4>Model answer</h4>
      <p>{feedback.model_answer}</p>
      <details>
        <summary>Reasoning, example and sources</summary>
        <ol>{feedback.reasoning.map((step, index) => <li key={index}>{step}</li>)}</ol>
        <p>{feedback.example}</p>
        {feedback.source_ids.map(id => {
          const source = job.reference.sources[id]
          return source?.startsWith('https://') && <p key={id}><a href={source} target="_blank" rel="noreferrer">{id}</a></p>
        })}
        {feedback.follow_up?.map((point, index) => <p key={index}>{point}</p>)}
      </details>
    </section>
  )
}

/** Read saved feedback and poll only while the displayed request is active. */
export function PracticeFeedback({ itemId, request, active, initial, answerLimit, navigationPending, leaveEditor, onStatus }: {
  itemId: string
  request: LocalRequest
  active: boolean
  initial?: SavedSubmission
  answerLimit: number
  navigationPending: boolean
  leaveEditor: RefObject<LeaveEditor | null>
  onStatus: (itemId: string, status: GradingStatus) => void
}) {
  const [{ submission, edit, replacement }, setView] = useState<FeedbackView>({ submission: initial })
  const [reload, setReload] = useState(0)
  const [readError, setReadError] = useState(false)
  const [actionError, setActionError] = useState('')
  const [busy, setBusy] = useState(false)
  const action = useRef<AbortController | undefined>(undefined)
  const actionVersion = useRef(0)
  const base = `/api/practice/items/${itemId}`
  const job = submission?.request

  useEffect(() => () => action.current?.abort(), [])

  useEffect(() => {
    if (!active) return
    const version = actionVersion.current
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    const refresh = async () => {
      try {
        const saved = await request<SavedSubmission | null>(`${base}/submission`, { signal: controller.signal })
        if (controller.signal.aborted || version !== actionVersion.current) return
        if (!saved) throw new Error('Submitted answer unavailable')
        setView(previous => {
          const draft = previous.submission?.clarification_draft
          const incomingDraft = saved.clarification_draft
          const sameClarification = previous.submission?.request.id === saved.request.id
            && saved.request.feedback?.status === 'needs_clarification' && incomingDraft !== null
          const dirty = previous.edit && previous.edit.text !== previous.edit.base.text
          // Keep the old editor available to copy until the learner discards the local text.
          if (previous.replacement || (!sameClarification && dirty)) {
            return { ...previous, replacement: saved }
          }
          if (sameClarification && draft && draft.version > incomingDraft.version) {
            return { ...previous, submission: { ...saved, clarification_draft: draft } }
          }
          return { submission: saved, edit: sameClarification ? previous.edit : undefined }
        })
        onStatus(itemId, saved.request.can_send ? 'ready_to_send' : saved.request.state)
        setReadError(false)
        if (isActive(saved.request)) timer = setTimeout(refresh, POLL_MS)
      } catch {
        if (!controller.signal.aborted && version === actionVersion.current) setReadError(true)
      }
    }
    // On reopening, load SQLite once even for settled answers. Later reads
    // continue only while this item's request can still change on its own.
    void refresh()
    return () => {
      clearTimeout(timer)
      controller.abort()
    }
  }, [base, request, active, reload, itemId, onStatus])

  function refresh() {
    setActionError('')
    setView(previous => previous.replacement ? { submission: previous.replacement } : previous)
    setReload(value => value + 1)
  }

  async function send(edit?: Draft) {
    if (!job || action.current) return
    const controller = new AbortController()
    action.current = controller
    actionVersion.current += 1
    setBusy(true)
    setActionError('')
    try {
      const saved = await request<SavedSubmission>(`${base}/retry`, {
        method: 'POST', signal: controller.signal, headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ request_id: job.id, ...edit, idempotency_key: crypto.randomUUID() }),
      })
      if (controller.signal.aborted) return
      setView({ submission: saved })
      onStatus(itemId, saved.request.can_send ? 'ready_to_send' : saved.request.state)
      refresh()
    } catch (error) {
      if (!controller.signal.aborted) {
        if (edit) throw error
        setActionError(error instanceof LocalApiError
          ? error.message : 'Could not confirm the grading action. Refresh the status before trying again.')
      }
    } finally {
      if (!controller.signal.aborted) {
        action.current = undefined
        setBusy(false)
      }
    }
  }

  return (
    <div className="today-editor practice-feedback">
      {(readError || actionError) && (
        <div className="actions">
          <p role="alert" className="error">{actionError || 'Could not refresh grading status.'}</p>
          <button type="button" disabled={busy || navigationPending} onClick={() => void (async () => {
            if (!leaveEditor.current || await leaveEditor.current()) refresh()
          })()}>Refresh grading status</button>
        </div>
      )}
      {!job && !readError && <p role="status">Loading submitted answer…</p>}
      {submission && job && (
        <>
          <p role="status" className="practice-credit">
            {submission.credited ? 'Practice counted on ' : 'This question already counted on '}
            <time dateTime={submission.completed_date}>{submission.completed_date}</time> ({submission.completed_timezone}).
          </p>
          {job.feedback?.status === 'assessed' ? <Assessment job={job} feedback={job.feedback} /> : (
            <>
              <p role="status"><strong>{REQUEST_LABELS[job.state]}</strong></p>
              {job.message && <p>{job.message}</p>}
              {job.state === 'awaiting_access' && <a href="#/settings">Open grading settings</a>}
              {(job.can_send || RECOVERABLE.has(job.state)) && (
                <div className="actions">
                  <button type="button" disabled={busy} onClick={() => void send()}>
                    {job.state === 'outcome_unknown' ? 'Retry grading' : 'Send saved answer'}
                  </button>
                </div>
              )}
              {job.feedback?.status === 'needs_clarification' && submission.clarification_draft && (
                <div className="practice-clarification">
                  <p className="muted">AI-generated feedback</p>
                  <p>{job.feedback.explanation}</p>
                  <ClarificationEditor key={job.id} draft={submission.clarification_draft} answerLimit={answerLimit}
                    edit={edit} closedByRefresh={replacement !== undefined}
                    onEdit={edit => setView(previous => ({ ...previous, edit }))}
                    leaveEditor={leaveEditor} navigationPending={navigationPending}
                    save={edit => request<Draft>(`${base}/clarification-draft`, {
                      method: 'PUT', headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify({ request_id: job.id, ...edit }),
                    })}
                    onSaved={draft => setView(previous => {
                      if (previous.submission?.request.id !== job.id) return previous
                      return { ...previous, submission: { ...previous.submission, clarification_draft: draft }, edit: undefined }
                    })}
                    send={send} refresh={refresh} />
                </div>
              )}
            </>
          )}
          <details open={!job.feedback || undefined}>
            <summary>Original answer</summary>
            <p className="practice-original">{submission.text}</p>
          </details>
          {submission.assessed_text !== submission.text && (
            <details>
              <summary>Most recently sent answer</summary>
              <p className="practice-original">{submission.assessed_text}</p>
            </details>
          )}
        </>
      )}
    </div>
  )
}
