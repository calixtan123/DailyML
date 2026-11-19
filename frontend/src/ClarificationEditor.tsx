import { type RefObject, useEffect, useRef, useState } from 'react'
import { LocalApiError } from './api'
import { type Draft, type LeaveEditor, savedAlternative, useUnsavedDraftWarning } from './drafts'

type SaveState =
  | { kind: 'idle' | 'saving' | 'saved' }
  | { kind: 'error'; message: string }
  | { kind: 'conflict'; saved: Draft }
  | { kind: 'closed' }

type Props = {
  draft: Draft
  answerLimit: number
  leaveEditor: RefObject<LeaveEditor | null>
  save: (edit: Draft) => Promise<Draft>
  onSaved: (draft: Draft) => void
  send: (draft: Draft) => Promise<void>
  refresh: () => void
}

/** Keep a clarification editable until its local save or grading action is confirmed. */
export function ClarificationEditor({ draft, answerLimit, leaveEditor, save, onSaved, send, refresh }: Props) {
  const [edit, setEdit] = useState<{ text: string; base: Draft }>()
  const [saveState, setSaveState] = useState<SaveState>({ kind: 'idle' })
  const [sendError, setSendError] = useState('')
  const [copyStatus, setCopyStatus] = useState('')
  const [busy, setBusy] = useState(false)
  const pending = useRef(false)
  const input = useRef<HTMLTextAreaElement>(null)
  const problem = useRef<HTMLParagraphElement>(null)
  const text = edit?.text ?? draft.text
  const dirty = edit !== undefined && text !== edit.base.text
  const count = Array.from(text).length
  const overLimit = count > answerLimit
  const blocked = saveState.kind === 'conflict' || saveState.kind === 'closed'

  useUnsavedDraftWarning(dirty)

  useEffect(() => {
    if (blocked || saveState.kind === 'error' || sendError) problem.current?.focus()
  }, [blocked, saveState, sendError])

  function acknowledge(saved: Draft, showSaved: boolean) {
    onSaved(saved)
    setEdit(undefined)
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
      setSaveState({ kind: 'error', message: 'Shorten your clarification before saving or switching questions.' })
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
        kind: 'error', message: 'Could not save your clarification draft. Your text is still here.',
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
        'Could not confirm grading. Your clarification draft is saved. You can refresh grading status or try sending again.',
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
      <label htmlFor="clarified-answer">Clarified answer</label>
      <textarea id="clarified-answer" ref={input} rows={6} value={text} readOnly={busy} spellCheck={false}
        aria-describedby="clarification-help clarification-limit" aria-invalid={overLimit || undefined}
        onChange={event => {
          setEdit({ text: event.target.value, base: edit?.base ?? draft })
          setCopyStatus('')
          if (saveState.kind === 'saved') setSaveState({ kind: 'idle' })
        }} />
      <div className="today-editor-caption">
        <p id="clarification-help">Save your draft here or switch questions to save it. Send when you’re ready for grading.</p>
        <p id="clarification-limit" className={overLimit ? 'error' : undefined} aria-live={overLimit ? 'polite' : 'off'}>
          {count.toLocaleString('en-GB')} / {answerLimit.toLocaleString('en-GB')} characters
        </p>
      </div>
      <div className="actions today-draft-actions">
        <button className="primary" type="button" disabled={busy || blocked || overLimit || !text.trim()}
          onClick={() => void run(sendAnswer)}>Send clarified answer</button>
        <button type="button" disabled={busy || blocked || overLimit || !dirty} onClick={() => void run(persist)}>
          {saveState.kind === 'error' ? 'Retry save' : 'Save draft'}
        </button>
        <p role="status" className="today-save-status">
          {saveState.kind === 'saving' && 'Saving draft…'}
          {saveState.kind === 'saved' && 'Draft saved.'}
          {saveState.kind === 'idle' && (dirty ? 'Unsaved changes' : draft.version > 0 && 'Saved draft loaded.')}
        </p>
      </div>
      {saveState.kind === 'error' && <p className="error" role="alert" tabIndex={-1} ref={problem}>{saveState.message}</p>}
      {sendError && (
        <div className="actions">
          <p className="error" role="alert" tabIndex={-1} ref={problem}>{sendError}</p>
          <button type="button" disabled={busy || dirty} onClick={refresh}>Refresh grading status</button>
        </div>
      )}
      {saveState.kind === 'conflict' && (
        <div className="today-conflict">
          <p role="alert" tabIndex={-1} ref={problem}>
            This draft changed in another tab. Review both versions before choosing which to keep.
          </p>
          <label htmlFor="saved-clarification">Saved in another tab</label>
          <textarea id="saved-clarification" readOnly rows={4} value={saveState.saved.text} />
          <div className="actions">
            <button type="button" disabled={busy} onClick={() => acknowledge(saveState.saved, false)}>Use saved version</button>
            <button type="button" disabled={busy || overLimit}
              onClick={() => void run(() => persist(saveState.saved.version))}>Save my version</button>
          </div>
        </div>
      )}
      {saveState.kind === 'closed' && (
        <div className="today-conflict">
          <p role="alert" tabIndex={-1} ref={problem}>
            This clarification is closed. Copy your text if you want to keep it.
          </p>
          <button type="button" onClick={() => {
            setEdit(undefined)
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
