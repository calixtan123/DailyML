import { type ReactNode, type RefObject, useEffect, useRef, useState } from 'react'
import { LocalApiError, type LocalRequest } from './api'
import { FollowUpEditor, type FollowUpEdit } from './FollowUpEditor'
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
export type GradingStatus = keyof typeof REQUEST_LABELS | 'ready_to_send' | 'self_reported_gap'
type GradingRequest = {
  id: string
  state: keyof typeof REQUEST_LABELS
  grade: number | null
  can_send: boolean
  message: string | null
  feedback: Feedback | null
  reference: { sources: Record<string, string> }
}
type GapRequest = {
  id: null
  state: 'self_reported_gap'
  grade: null
  can_send: false
  message: null
  feedback: null
  reference: null
}

type AnswerTrack = {
  assessed_text: string
  request: GradingRequest
  clarification_draft: Draft | null
  submission_version_id: string
  supported: boolean
}

type SubmissionVersion = {
  id: string
  role: 'original' | 'revision'
  kind: 'initial' | 'clarification' | 'revision'
  parent_version_id: string | null
  text: string
  supported: boolean
  created_at: number
  request: GradingRequest
}

export type SavedSubmission = Omit<AnswerTrack, 'request' | 'submission_version_id' | 'supported'> & {
  request: GradingRequest | GapRequest
  submission_version_id?: string
  supported?: boolean
  assisted?: boolean
  text: string
  completed_date: string
  completed_timezone: string
  credited: boolean
  versions: SubmissionVersion[]
  revision: AnswerTrack | null
  revision_draft: Draft | null
}

type FollowUp = {
  kind: 'clarification' | 'revision'
  track: 'original' | 'revision'
  requestId: string
  draft: Draft
}

type FeedbackView = {
  submission?: SavedSubmission
  editingRevision?: boolean
  edit?: FollowUpEdit
  replacement?: SavedSubmission
}

function followUp({ submission, editingRevision }: FeedbackView): FollowUp | undefined {
  if (!submission) return
  const track = submission.revision ? 'revision' : 'original'
  const answer = submission.revision ?? submission
  if (answer.request.state !== 'self_reported_gap' && answer.request.feedback?.status === 'needs_clarification' && answer.clarification_draft) {
    return { kind: 'clarification', track, requestId: answer.request.id, draft: answer.clarification_draft }
  }
  if (submission.request.state !== 'self_reported_gap' && editingRevision && !submission.revision && submission.revision_draft) {
    return { kind: 'revision', track: 'revision', requestId: submission.request.id, draft: submission.revision_draft }
  }
}

function withDraft(submission: SavedSubmission, editor: FollowUp, draft: Draft): SavedSubmission {
  if (editor.kind === 'revision') return { ...submission, revision_draft: draft }
  if (editor.track === 'revision' && submission.revision) {
    return { ...submission, revision: { ...submission.revision, clarification_draft: draft } }
  }
  return { ...submission, clarification_draft: draft }
}

function mergeRead(previous: FeedbackView, saved: SavedSubmission): FeedbackView {
  const shown = followUp(previous)
  const incoming = followUp({ ...previous, submission: saved })
  const sameEditor = incoming !== undefined && shown?.kind === incoming.kind && shown.requestId === incoming.requestId
  const dirty = previous.edit && previous.edit.text !== previous.edit.base.text
  // A different request may have closed the editor in another tab. Retain local text for copying.
  if (previous.replacement || (!sameEditor && dirty)) return { ...previous, replacement: saved }
  return {
    submission: sameEditor && shown.draft.version > incoming.draft.version
      ? withDraft(saved, incoming, shown.draft) : saved,
    editingRevision: sameEditor && previous.editingRevision,
    edit: sameEditor ? previous.edit : undefined,
  }
}

const POLL_MS = 500
const RECOVERABLE = new Set<GradingRequest['state']>([
  'awaiting_access', 'awaiting_allowance', 'outcome_unknown', 'failed_recoverable',
])

function isActive(job: GradingRequest | GapRequest | undefined) {
  return job?.state === 'in_flight' || (job?.state === 'queued' && !job.can_send)
}

function Assessment({ job, feedback }: { job: GradingRequest; feedback: Extract<Feedback, { status: 'assessed' }> }) {
  return (
    <section className="grading-result" aria-label="AI-generated feedback">
      <h4>AI-generated feedback</h4>
      <p role="status"><strong>Grade {job.grade} of 3</strong></p>
      <h5>What you understood</h5>
      <p>{feedback.summary}</p>
      {feedback.corrections.length > 0 && (
        <>
          <h5>What to improve</h5>
          <ul>{feedback.corrections.map((point, index) => <li key={index}>{point}</li>)}</ul>
        </>
      )}
      <h5>Model answer</h5>
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

function RequestFeedback({ job, disabled, retry, children }: {
  job: GradingRequest
  disabled: boolean
  retry: () => void
  children?: ReactNode
}) {
  if (job.feedback?.status === 'assessed') return <Assessment job={job} feedback={job.feedback} />
  return (
    <>
      <p role="status"><strong>{REQUEST_LABELS[job.state]}</strong></p>
      {job.message && <p>{job.message}</p>}
      {job.state === 'awaiting_access' && <a href="#/settings">Open grading settings</a>}
      {(job.can_send || RECOVERABLE.has(job.state)) && (
        <div className="actions">
          <button type="button" disabled={disabled} onClick={retry}>
            {job.state === 'outcome_unknown' ? 'Retry grading' : 'Send saved answer'}
          </button>
        </div>
      )}
      {job.feedback?.status === 'needs_clarification' && (
        <>
          <p className="muted">AI-generated feedback</p>
          <p>{job.feedback.explanation}</p>
          {children}
        </>
      )}
    </>
  )
}

function AnswerText({ label, text, open }: { label: string; text: string; open?: boolean }) {
  return (
    <details open={open}>
      <summary>{label}</summary>
      <p className="practice-original">{text}</p>
    </details>
  )
}

/** Read saved feedback and poll while the latest answer's grading request is active. */
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
  const [view, setView] = useState<FeedbackView>({ submission: initial })
  const [reload, setReload] = useState(0)
  const [readError, setReadError] = useState(false)
  const [actionError, setActionError] = useState('')
  const [busy, setBusy] = useState(false)
  const action = useRef<AbortController | undefined>(undefined)
  const actionVersion = useRef(0)
  const lastRead = useRef(initial)
  const focusAfterSend = useRef(false)
  const originalHeading = useRef<HTMLHeadingElement>(null)
  const revisionHeading = useRef<HTMLHeadingElement>(null)
  const base = `/api/practice/items/${itemId}`
  const { submission, edit, replacement } = view
  const original = submission?.request
  const revision = submission?.revision
  const editor = followUp(view)
  const disabled = busy || navigationPending

  useEffect(() => () => action.current?.abort(), [])

  useEffect(() => {
    if (!focusAfterSend.current) return
    focusAfterSend.current = false
    const heading = revision ? revisionHeading : originalHeading
    heading.current?.focus()
  }, [submission, revision])

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
        lastRead.current = saved
        setView(previous => mergeRead(previous, saved))
        const job = saved.revision?.request ?? saved.request
        onStatus(itemId, job.can_send ? 'ready_to_send' : job.state)
        setReadError(false)
        if (isActive(job)) timer = setTimeout(refresh, POLL_MS)
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

  async function send(requestId: string, route: 'retry' | 'revise', draft?: Draft) {
    if (action.current) return
    const controller = new AbortController()
    action.current = controller
    actionVersion.current += 1
    setBusy(true)
    setActionError('')
    try {
      const payload = { request_id: requestId, ...draft }
      const saved = await request<SavedSubmission>(`${base}/${route}`, {
        method: 'POST', signal: controller.signal, headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(route === 'retry' ? { ...payload, idempotency_key: crypto.randomUUID() } : payload),
      })
      if (controller.signal.aborted) return
      lastRead.current = saved
      focusAfterSend.current = true
      setView({ submission: saved })
      const job = saved.revision?.request ?? saved.request
      onStatus(itemId, job.can_send ? 'ready_to_send' : job.state)
      refresh()
    } catch (error) {
      if (!controller.signal.aborted) {
        if (draft) throw error
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

  async function saveDraft(editor: FollowUp, draft: Draft) {
    const saved = await request<Draft>(`${base}/${editor.kind}-draft`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ request_id: editor.requestId, ...draft }),
    })
    // A status read can observe a competing write before this save's response reaches the tab.
    if (lastRead.current) {
      const latest = followUp({ submission: lastRead.current, editingRevision: editor.kind === 'revision' })
      if (!latest || latest.requestId !== editor.requestId || latest.kind !== editor.kind) {
        throw new LocalApiError(409, null)
      }
      if (latest.draft.version > saved.version) {
        throw new LocalApiError(409, { saved: latest.draft })
      }
    }
    return saved
  }

  const followUpEditor = editor && (
    <FollowUpEditor key={`${editor.kind}-${editor.requestId}`} kind={editor.kind} draft={editor.draft}
      edit={edit} closedByRefresh={replacement !== undefined}
      onEdit={edit => setView(previous => ({ ...previous, edit }))}
      answerLimit={answerLimit} leaveEditor={leaveEditor} navigationPending={navigationPending}
      save={draft => saveDraft(editor, draft)}
      onSaved={draft => setView(previous => {
        if (!previous.submission || followUp(previous)?.requestId !== editor.requestId) return previous
        return { ...previous, submission: withDraft(previous.submission, editor, draft), edit: undefined }
      })}
      send={draft => send(editor.requestId, editor.kind === 'revision' ? 'revise' : 'retry', draft)} refresh={refresh} />
  )
  const initialRevision = revision && submission?.versions.find(version => version.kind === 'revision')

  return (
    <div className="today-editor practice-feedback">
      {(readError || actionError) && (
        <div className="actions">
          <p role="alert" className="error">{actionError || 'Could not refresh grading status.'}</p>
          <button type="button" disabled={disabled} onClick={() => void (async () => {
            if (!leaveEditor.current || await leaveEditor.current()) refresh()
          })()}>Refresh grading status</button>
        </div>
      )}
      {!original && !readError && <p role="status">Loading submitted answer…</p>}
      {submission && original && (
        <>
          <p role="status" className="practice-credit">
            {submission.credited ? 'Practice counted on ' : 'This question already counted on '}
            <time dateTime={submission.completed_date}>{submission.completed_date}</time> ({submission.completed_timezone}).
          </p>
          {original.state === 'self_reported_gap' ? (
            <>
              <p role="status"><strong>Self-reported gap</strong> — no answer was sent for AI grading.</p>
              <AnswerText label="Original answer" text={submission.text} open />
            </>
          ) : <section className="practice-attempt" aria-labelledby="original-attempt">
            <h3 id="original-attempt" ref={originalHeading} tabIndex={-1}>Original attempt</h3>
            <AnswerText label="Original answer" text={submission.text} open={Boolean(revision) || original.feedback?.status !== 'assessed'} />
            {submission.assessed_text !== submission.text && (
              <AnswerText label="Clarified original answer" text={submission.assessed_text} open />
            )}
            <RequestFeedback job={original} disabled={disabled} retry={() => void send(original.id, 'retry')}>
              {editor?.track === 'original' && followUpEditor}
            </RequestFeedback>
            {submission.assisted && <p className="muted">Assisted practice — help was viewed before this answer was submitted.</p>}
          </section>}
          {revision && (
            <section className="practice-attempt" aria-labelledby="assisted-revision">
              <h3 id="assisted-revision" ref={revisionHeading} tabIndex={-1}>Assisted revision</h3>
              {initialRevision && initialRevision.id !== revision.submission_version_id && (
                <AnswerText label="First revised answer" text={initialRevision.text} />
              )}
              <AnswerText label={initialRevision && initialRevision.id !== revision.submission_version_id
                ? 'Clarified revision' : 'Revised answer'} text={revision.assessed_text} open />
              <RequestFeedback job={revision.request} disabled={disabled} retry={() => void send(revision.request.id, 'retry')}>
                {editor?.track === 'revision' && followUpEditor}
              </RequestFeedback>
            </section>
          )}
          {!revision && submission.revision_draft && (
            <div className="practice-attempt">
              {editor?.kind === 'revision' ? (
                <>
                  <p className="muted">You can send one assisted revision. Your original review date and practice credit are kept.</p>
                  {followUpEditor}
                </>
              ) : (
                <div className="actions">
                  <button type="button" disabled={disabled}
                    onClick={() => setView(previous => ({ ...previous, editingRevision: true }))}>
                    Revise answer
                  </button>
                </div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  )
}
