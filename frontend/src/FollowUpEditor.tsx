import { type RefObject, useEffect, useRef, useState } from 'react'
import { LocalApiError } from './api'
import { type Draft, type LeaveEditor, savedAlternative, useUnsavedDraftWarning } from './drafts'

type SaveState =
  | { kind: 'idle' | 'saving' | 'saved' }
  | { kind: 'error'; message: string }
  | { kind: 'conflict'; saved: Draft }
  | { kind: 'closed' }

export type FollowUpEdit = { text: string; base: Draft }

type Props = {
  kind: 'clarification' | 'revision'
  draft: Draft
  edit: FollowUpEdit | undefined
  closedByRefresh: boolean
  onEdit: (edit: FollowUpEdit | undefined) => void
  answerLimit: number
  navigationPending: boolean
  leaveEditor: RefObject<LeaveEditor | null>
  save: (edit: Draft) => Promise<Draft>
  onSaved: (draft: Draft) => void
  send: (draft: Draft) => Promise<void>
  refresh: () => void
}

/** Keep a follow-up answer editable until its save or grading action is confirmed. */
export function FollowUpEditor({
  kind, draft, edit, closedByRefresh, onEdit, answerLimit, leaveEditor, navigationPending, save, onSaved, send, refresh,
}: Props) {
  const [saveState, setSaveState] = useState<SaveState>({ kind: 'idle' })
  const [sendError, setSendError] = useState('')
  const [copyStatus, setCopyStatus] = useState('')
  const [busy, setBusy] = useState(false)
  const pending = useRef(false)
  const input = useRef<HTMLTextAreaElement>(null)
  const problem = useRef<HTMLParagraphElement>(null)
  const answerLabel = kind === 'revision' ? 'Revised answer' : 'Clarified answer'
  const text = edit?.text ?? draft.text
  const dirty = edit !== undefined && text !== edit.base.text
  const count = Array.from(text).length
  const overLimit = count > answerLimit
  const closed = closedByRefresh || saveState.kind === 'closed'
  const blocked = saveState.kind === 'conflict' || closed
  const actionPending = busy || navigationPending

  useUnsavedDraftWarning(dirty)

  useEffect(() => {
    if (kind === 'revision') input.current?.focus()
  }, [kind])

  useEffect(() => {
    if (blocked || saveState.kind === 'error' || sendError) problem.current?.focus()
  }, [blocked, closed, saveState, sendError])

  function acknowledge(saved: Draft, showSaved: boolean) {
    onSaved(saved)
    setCopyStatus('')
    setSaveState({ kind: showSaved ? 'saved' : 'idle' })
  }

  function showConflict(error: unknown) {
    const alternative = savedAlternative(error)
    if (alternative) {
      setSaveState({ kind: 'conflict', saved: alternative })
      return true
    }
    if (error instanceof LocalApiError && error.status === 409) {
      setSaveState({ kind: 'closed' })
      return true
    }
    return false
  }

  async function persist(version = edit?.base.version ?? draft.version): Promise<Draft | undefined> {
    if (overLimit) {
      setSaveState({ kind: 'error', message: `Shorten your ${kind} before saving or switching questions.` })
      return
    }
    setSaveState({ kind: 'saving' })
    setCopyStatus('')
    setSendError('')
    try {
      const saved = await save({ text, version })
      acknowledge(saved, true)
      return saved
    } catch (error) {
      if (!showConflict(error)) setSaveState({
        kind: 'error', message: `Could not save your ${kind} draft. Your text is still here.`,
      })
    }
  }

  async function run<T>(action: () => Promise<T>): Promise<T | undefined> {
    if (pending.current) return
    pending.current = true
    setBusy(true)
    try {
      return await action()
    } finally {
      pending.current = false
      setBusy(false)
    }
  }

  useEffect(() => {
    leaveEditor.current = async () => {
      if (pending.current) return false
      if (blocked) return false
      if (!dirty) return true
      return Boolean(await run(persist))
    }
    return () => { leaveEditor.current = null }
  })

  async function sendAnswer() {
    const saved = dirty ? await persist() : draft
    if (!saved) return
    setSendError('')
    try {
      await send(saved)
    } catch (error) {
      if (!showConflict(error)) setSendError(
        `Could not confirm grading. Your ${kind} draft is saved. You can refresh grading status or try sending again.`,
      )
    }
  }

  async function copyAnswer() {
    try {
      await navigator.clipboard.writeText(text)
      setCopyStatus('Answer copied.')
    } catch {
      input.current?.focus()
      input.current?.select()
      setCopyStatus('Your answer is selected. Use your browser’s Copy command.')
    }
  }

  return (
    <>
      <label htmlFor="follow-up-answer">{answerLabel}</label>
      <textarea id="follow-up-answer" ref={input} rows={6} value={text} readOnly={actionPending || closed} spellCheck={false}
        aria-describedby={`${closed ? 'follow-up-closed' : 'follow-up-help'} follow-up-limit`}
        aria-invalid={overLimit || undefined}
        onChange={event => {
          if (actionPending || closed) return
          onEdit({ text: event.target.value, base: edit?.base ?? draft })
          setCopyStatus('')
          if (saveState.kind === 'saved') setSaveState({ kind: 'idle' })
        }} />
      <div className="today-editor-caption">
        {!closed && <p id="follow-up-help">
          Save your draft here or switch questions to save it. Send when you’re ready for grading.
        </p>}
        <p id="follow-up-limit" className={overLimit ? 'error' : undefined} aria-live={overLimit ? 'polite' : 'off'}>
          {count.toLocaleString('en-GB')} / {answerLimit.toLocaleString('en-GB')} characters
        </p>
      </div>
      <div className="actions today-draft-actions">
        <button className="primary" type="button" disabled={actionPending || blocked || overLimit || !text.trim()}
          onClick={() => void run(sendAnswer)}>{kind === 'revision' ? 'Send revised answer' : 'Send clarified answer'}</button>
        <button type="button" disabled={actionPending || blocked || overLimit || !dirty} onClick={() => void run(persist)}>
          {saveState.kind === 'error' ? 'Retry save' : 'Save draft'}
        </button>
        <p role="status" className="today-save-status">
          {saveState.kind === 'saving' && 'Saving draft…'}
          {saveState.kind === 'saved' && 'Draft saved.'}
          {saveState.kind === 'idle' && (dirty ? 'Unsaved changes' : draft.version > 0 && 'Saved draft loaded.')}
        </p>
      </div>
      {!closed && saveState.kind === 'error' && <p className="error" role="alert" tabIndex={-1} ref={problem}>{saveState.message}</p>}
      {!closed && sendError && (
        <div className="actions">
          <p className="error" role="alert" tabIndex={-1} ref={problem}>{sendError}</p>
          <button type="button" disabled={actionPending || dirty} onClick={refresh}>Refresh grading status</button>
        </div>
      )}
      {!closed && saveState.kind === 'conflict' && (
        <div className="today-conflict">
          <p role="alert" tabIndex={-1} ref={problem}>
            This draft changed in another tab. Review both versions before choosing which to keep.
          </p>
          <label htmlFor="saved-follow-up">Saved in another tab</label>
          <textarea id="saved-follow-up" readOnly rows={4} value={saveState.saved.text} />
          <div className="actions">
            <button type="button" disabled={actionPending} onClick={() => acknowledge(saveState.saved, false)}>Use saved version</button>
            <button type="button" disabled={actionPending || overLimit}
              onClick={() => void run(() => persist(saveState.saved.version))}>Save my version</button>
          </div>
        </div>
      )}
      {closed && (
        <div className="today-conflict">
          <p id="follow-up-closed" role="alert" tabIndex={-1} ref={problem}>
            This {kind} is closed. Copy your text if you want to keep it.
          </p>
          <button type="button" disabled={actionPending} onClick={() => {
            onEdit(undefined)
            setSaveState({ kind: 'idle' })
            refresh()
          }}>Discard edit and refresh grading status</button>
        </div>
      )}
      {(overLimit || blocked || saveState.kind === 'error') && (
        <div className="actions today-copy">
          <button type="button" onClick={() => void copyAnswer()}>Copy my answer</button>
          <span role="status">{copyStatus}</span>
        </div>
      )}
    </>
  )
}
