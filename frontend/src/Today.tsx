import { CalendarDays, Plus } from 'lucide-react'
import { type RefObject, useEffect, useRef, useState } from 'react'
import { LocalApiError, type LocalRequest } from './api'
import { QuestionBrowser } from './QuestionBrowser'

type Draft = { text: string; version: number }
type PracticeItem = {
  id: string
  local_date: string
  question: {
    title: string
    prompt: string
    objective: string
    section_title: string
    difficulty: string
    expected_answer_words: [number, number]
  }
  draft: Draft
}
type Selection = { item: PracticeItem; kind: 'review' | 'variety' | 'extra' | 'focused' }
type Session = {
  local_date: string
  timezone: string
  effective_timezone: string
  goal: number
  answer_limit: number
  shortage: string | null
  selections: Selection[]
  unfinished: PracticeItem[]
}
type Addition = { today: Session; item_id: string | null; message: string | null }
type Choice = { section_id?: string; question_id?: string }
type Editor = { item: PracticeItem; text: string }
type SaveState =
  | { kind: 'idle' | 'saving' | 'saved' }
  | { kind: 'error'; message: string }
  | { kind: 'conflict'; saved: Draft }
export type LeaveToday = (next: () => void) => void

const DATE_CHECK_MS = 60_000

function jsonOptions(method: 'POST' | 'PUT', body: Draft | Choice): RequestInit {
  return { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
}

function currentLocalDate(timeZone: string) {
  const parts = new Intl.DateTimeFormat('en-GB', {
    timeZone, year: 'numeric', month: '2-digit', day: '2-digit',
  }).formatToParts(Date.now())
  const part = (type: string) => parts.find(value => value.type === type)?.value
  return `${part('year')}-${part('month')}-${part('day')}`
}

function savedAlternative(error: unknown): Draft | undefined {
  if (!(error instanceof LocalApiError) || error.status !== 409) return undefined
  const draft = error.payload?.saved as Partial<Draft> | undefined
  if (!draft || typeof draft.text !== 'string' || !Number.isInteger(draft.version)) return undefined
  return draft as Draft
}

function updateDraft(session: Session, id: string, draft: Draft): Session {
  const update = (item: PracticeItem) => item.id === id ? { ...item, draft } : item
  return {
    ...session,
    selections: session.selections.map(selection => ({ ...selection, item: update(selection.item) })),
    unfinished: session.unfinished.map(update),
  }
}

/** Manage a local editing buffer and switch questions only after every dirty edit is saved. */
export function Today({ request, active, leaveToday }: {
  request: LocalRequest
  active: boolean
  leaveToday: RefObject<LeaveToday | null>
}) {
  const [session, setSession] = useState<Session>()
  const [editor, setEditor] = useState<Editor>()
  const [loadError, setLoadError] = useState(false)
  const [loadAttempt, setLoadAttempt] = useState(0)
  const [saveState, setSaveState] = useState<SaveState>({ kind: 'idle' })
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState('')
  const [actionError, setActionError] = useState('')
  const [copyStatus, setCopyStatus] = useState('')
  const [browsing, setBrowsing] = useState(false)
  const operation = useRef(false)
  const operationVersion = useRef(0)
  const heading = useRef<HTMLHeadingElement>(null)
  const focusQuestion = useRef(false)
  const answer = useRef<HTMLTextAreaElement>(null)
  const dirty = editor !== undefined && editor.text !== editor.item.draft.text
  const characterCount = Array.from(editor?.text ?? '').length
  const overLimit = session !== undefined && characterCount > session.answer_limit

  useEffect(() => {
    if (!active) return
    const controller = new AbortController()
    let lastSession: Session | undefined
    let loading = false

    async function refresh() {
      if (loading || operation.current) return
      loading = true
      const version = operationVersion.current
      try {
        const today = await request<Session>('/api/practice/today', { method: 'POST', signal: controller.signal })
        if (controller.signal.aborted || version !== operationVersion.current) return
        lastSession = today
        setSession(today)
        setLoadError(false)
        const first = today.selections[0]?.item ?? today.unfinished[0]
        if (first) setEditor(previous => previous ?? { item: first, text: first.draft.text })
      } catch {
        if (!controller.signal.aborted) setLoadError(true)
      } finally {
        loading = false
      }
    }

    function checkDate() {
      if (!lastSession || currentLocalDate(lastSession.effective_timezone) !== lastSession.local_date) void refresh()
    }

    void refresh()
    const timer = window.setInterval(checkDate, DATE_CHECK_MS)
    window.addEventListener('focus', checkDate)
    return () => {
      controller.abort()
      window.clearInterval(timer)
      window.removeEventListener('focus', checkDate)
    }
  }, [request, active, loadAttempt])

  useEffect(() => {
    if (!dirty) return
    function warnOnUnload(event: BeforeUnloadEvent) {
      event.preventDefault()
      event.returnValue = true
    }
    window.addEventListener('beforeunload', warnOnUnload)
    return () => window.removeEventListener('beforeunload', warnOnUnload)
  }, [dirty])

  useEffect(() => {
    if (!focusQuestion.current) return
    focusQuestion.current = false
    heading.current?.focus()
  }, [editor?.item.id])

  function showItem(item: PracticeItem) {
    if (editor?.item.id !== item.id) focusQuestion.current = true
    setEditor({ item, text: item.draft.text })
    setSaveState({ kind: 'idle' })
    setCopyStatus('')
  }

  function acknowledge(draft: Draft) {
    if (!editor) return
    const item = { ...editor.item, draft }
    setEditor({ item, text: draft.text })
    setSession(previous => previous && updateDraft(previous, item.id, draft))
    setCopyStatus('')
  }

  async function persistDraft(version = editor?.item.draft.version): Promise<boolean> {
    if (!editor || version === undefined) return true
    if (overLimit) {
      setSaveState({ kind: 'error', message: 'Shorten your answer before saving or switching questions.' })
      answer.current?.focus()
      return false
    }
    setSaveState({ kind: 'saving' })
    setCopyStatus('')
    try {
      const draft = await request<Draft>(
        `/api/practice/items/${editor.item.id}/draft`,
        jsonOptions('PUT', { text: editor.text, version }),
      )
      acknowledge(draft)
      setSaveState({ kind: 'saved' })
      return true
    } catch (error) {
      const saved = savedAlternative(error)
      setSaveState(saved ? { kind: 'conflict', saved } : {
        kind: 'error', message: 'Could not save your draft. Your text is still here.',
      })
      return false
    }
  }

  async function run(action: () => void | Promise<void>) {
    if (operation.current) return
    operation.current = true
    operationVersion.current += 1
    setBusy(true)
    try {
      await action()
    } finally {
      operation.current = false
      setBusy(false)
    }
  }

  async function afterDraftSaved(action: () => void | Promise<void>) {
    await run(async () => {
      if (saveState.kind === 'conflict' && dirty) return
      if (dirty && !await persistDraft()) return
      setActionError('')
      await action()
    })
  }

  useEffect(() => {
    leaveToday.current = (next) => { void afterDraftSaved(next) }
    return () => { leaveToday.current = null }
  })

  async function addQuestion(choice: Choice) {
    await afterDraftSaved(async () => {
      setNotice('')
      try {
        const result = await request<Addition>('/api/practice/items', jsonOptions('POST', choice))
        setSession(result.today)
        const item = result.today.selections.find(selection => selection.item.id === result.item_id)?.item
        if (item) {
          showItem(item)
          setBrowsing(false)
        }
        setNotice(result.message ?? '')
      } catch {
        setActionError('Could not open another question. Try again.')
      }
    })
  }

  async function copyAnswer() {
    if (!editor) return
    try {
      await navigator.clipboard.writeText(editor.text)
      setCopyStatus('Answer copied.')
    } catch {
      answer.current?.focus()
      answer.current?.select()
      setCopyStatus('Your answer is selected. Use your browser’s Copy command.')
    }
  }

  function selectItem(item: PracticeItem) {
    if (item.id === editor?.item.id) return
    void afterDraftSaved(() => {
      showItem(item)
      setNotice('')
    })
  }

  function retryToday() {
    setLoadError(false)
    setLoadAttempt(previous => previous + 1)
  }

  const selections = session?.selections ?? []
  const unfinished = session?.unfinished ?? []
  const picks = [...selections.map(selection => selection.item), ...unfinished]
  const previousItem = editor && !picks.some(item => item.id === editor.item.id) ? editor.item : undefined
  const resumable = previousItem ? [previousItem, ...unfinished] : unfinished
  const controlsDisabled = !session || busy || saveState.kind === 'conflict'
  const question = editor?.item.question

  return (
    <>
      <h1>Today</h1>
      {!session && !loadError && <p className="panel" role="status">Loading saved practice…</p>}
      {loadError && (
        <div className="panel today-load-error">
          <p role="alert">Could not load today’s saved practice.</p>
          <button type="button" onClick={retryToday}>Retry loading</button>
        </div>
      )}
      {session && (
        <div className="panel today-date">
          <CalendarDays size={19} aria-hidden="true" />
          <time dateTime={session.local_date}>
            {new Intl.DateTimeFormat('en-GB', {
              timeZone: 'UTC', weekday: 'long', day: 'numeric', month: 'long',
            }).format(new Date(session.local_date))}
          </time>
          <span className="muted">Daily goal: {session.goal} questions</span>
        </div>
      )}
      {session?.shortage && <p className="notice today-shortage" role="status">{session.shortage}</p>}
      <div className="today-grid">
        <aside className="panel today-session" aria-labelledby="session-heading">
          <h2 id="session-heading">Today’s session</h2>
          {picks.length > 0 && (
            <>
              <div className="today-compact">
                <label htmlFor="session-question">Session question</label>
                <select id="session-question" value={editor?.item.id ?? ''} disabled={controlsDisabled}
                  onChange={event => {
                    const item = [...picks, ...(previousItem ? [previousItem] : [])]
                      .find(item => item.id === event.target.value)
                    if (item) selectItem(item)
                  }}>
                  {selections.map(({ item }) => <option key={item.id} value={item.id}>{item.question.title}</option>)}
                  {resumable.length > 0 && (
                    <optgroup label="Resume practice">
                      {resumable.map(item => <option key={item.id} value={item.id}>{item.question.title}</option>)}
                    </optgroup>
                  )}
                </select>
              </div>
              <div className="today-session-list">
                <ol>
                  {selections.map(({ item, kind }, index) => (
                    <li key={item.id}>
                      <button type="button" disabled={controlsDisabled} onClick={() => selectItem(item)}
                        aria-current={editor?.item.id === item.id ? 'true' : undefined}>
                        <span className="session-order" aria-hidden="true">{index + 1}</span>
                        <span>{item.question.title}<span className="session-kind">
                          {{ review: 'Due review', variety: 'Recommendation', extra: 'Extra practice', focused: 'Focused practice' }[kind]}
                        </span></span>
                      </button>
                    </li>
                  ))}
                </ol>
                {resumable.length > 0 && (
                  <>
                    <h3>Resume practice</h3>
                    <ul>
                      {resumable.map(item => (
                        <li key={item.id}>
                          <button type="button" disabled={controlsDisabled} onClick={() => selectItem(item)}
                            aria-current={editor?.item.id === item.id ? 'true' : undefined}>
                            <span>{item.question.title}<span className="session-kind">{item.local_date}</span></span>
                          </button>
                        </li>
                      ))}
                    </ul>
                  </>
                )}
              </div>
            </>
          )}
          <button className="today-more" type="button" disabled={controlsDisabled} onClick={() => void addQuestion({})}>
            <Plus size={17} aria-hidden="true" /> More questions
          </button>
          {notice && <p className="notice" role="status">{notice}</p>}
          {actionError && <p className="error" role="alert">{actionError}</p>}
        </aside>
        {session && editor && question && (
          <article className="panel today-question" aria-labelledby="practice-question-title">
            <header className="today-prompt">
              <p className="today-metadata">{question.section_title} <span aria-hidden="true">·</span> {question.difficulty}</p>
              <h2 tabIndex={-1} ref={heading} id="practice-question-title">{question.title}</h2>
              <p>{question.prompt}</p>
              <p className="today-objective">{question.objective}</p>
            </header>
            <div className="today-editor">
              <div className="today-editor-label">
                <label htmlFor="practice-answer">Your answer</label>
                <span className="muted">{question.expected_answer_words.join('–')} words suggested</span>
              </div>
              <textarea id="practice-answer" ref={answer} rows={8} value={editor.text} readOnly={busy}
                spellCheck={false} aria-describedby="draft-help draft-limit" aria-invalid={overLimit || undefined}
                onChange={event => {
                  setEditor({ ...editor, text: event.target.value })
                  setCopyStatus('')
                  if (saveState.kind === 'saved') setSaveState({ kind: 'idle' })
                }} />
              <div className="today-editor-caption">
                <p id="draft-help">Drafts save here or when you switch questions.</p>
                <p id="draft-limit" className={overLimit ? 'error' : undefined} aria-live={overLimit ? 'polite' : 'off'}>
                  {characterCount.toLocaleString('en-GB')} / {session.answer_limit.toLocaleString('en-GB')} characters
                </p>
              </div>
              <div className="actions today-draft-actions">
                <button className="primary" type="button"
                  disabled={!dirty || busy || overLimit || saveState.kind === 'conflict'}
                  onClick={() => void run(async () => { await persistDraft() })}>
                  {saveState.kind === 'error' ? 'Retry save' : 'Save draft'}
                </button>
                <p role="status" className="today-save-status">
                  {saveState.kind === 'saving' && 'Saving draft…'}
                  {saveState.kind === 'saved' && 'Draft saved.'}
                  {saveState.kind === 'idle' && (dirty ? 'Unsaved changes' : editor.item.draft.version > 0 && 'Saved draft loaded.')}
                </p>
              </div>
              {saveState.kind === 'error' && <p role="alert" className="error">{saveState.message}</p>}
              {saveState.kind === 'conflict' && (
                <div className="today-conflict">
                  <p role="alert">This draft changed in another tab. Review both versions before choosing which to keep.</p>
                  <label htmlFor="saved-alternative">Saved in another tab</label>
                  <textarea id="saved-alternative" readOnly rows={4} value={saveState.saved.text} />
                  <div className="actions">
                    <button type="button" disabled={busy} onClick={() => {
                      acknowledge(saveState.saved)
                      setSaveState({ kind: 'idle' })
                    }}>Use saved version</button>
                    <button type="button" disabled={busy || overLimit} onClick={() => {
                      void run(async () => { await persistDraft(saveState.saved.version) })
                    }}>Save my version</button>
                  </div>
                </div>
              )}
              {(saveState.kind === 'error' || saveState.kind === 'conflict' || overLimit) && (
                <div className="actions today-copy">
                  <button type="button" onClick={() => void copyAnswer()}>Copy my answer</button>
                  <span role="status">{copyStatus}</span>
                </div>
              )}
            </div>
          </article>
        )}
      </div>
      <details className="today-bank" open={browsing} onToggle={event => setBrowsing(event.currentTarget.open)}>
        <summary>Browse questions</summary>
        {browsing && <QuestionBrowser request={request} onPractice={choice => void addQuestion(choice)}
          practiceDisabled={controlsDisabled} />}
      </details>
    </>
  )
}
