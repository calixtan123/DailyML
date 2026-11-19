import { useEffect } from 'react'
import { LocalApiError } from './api'

export type Draft = { text: string; version: number }
export type LeaveEditor = () => Promise<boolean>

export function savedAlternative(error: unknown): Draft | undefined {
  if (!(error instanceof LocalApiError) || error.status !== 409) return undefined
  const draft = error.payload?.saved as Partial<Draft> | undefined
  if (!draft || typeof draft.text !== 'string' || !Number.isInteger(draft.version)) return undefined
  return draft as Draft
}

export function useUnsavedDraftWarning(dirty: boolean) {
  useEffect(() => {
    if (!dirty) return
    function warnOnUnload(event: BeforeUnloadEvent) {
      event.preventDefault()
      event.returnValue = true
    }
    window.addEventListener('beforeunload', warnOnUnload)
    return () => window.removeEventListener('beforeunload', warnOnUnload)
  }, [dirty])
}
