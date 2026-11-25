import { ExternalLink } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import type { LocalRequest } from './api'

const LABELS = {
  signed_out: 'Signed out',
  signing_in: 'Signing in',
  connected: 'Connected',
  renewal_needed: 'Access renewal needed',
  reconnect: 'Reconnect required',
  declined: 'Authorization declined',
  cancelled: 'Sign-in cancelled',
  timeout: 'Sign-in timed out',
  error: 'Sign-in failed',
} as const

type Status = {
  state: keyof typeof LABELS
  email: string | null
  plan_authorized: boolean
  access_token_usable: boolean
  reauthorization_required: boolean
  message: string | null
  remote_revocation_unconfirmed: boolean
  requires_consent: boolean
}

/** Verified grading account, null for no access, or undefined when status is unavailable. */
export type GradingAccount = string | null | undefined

const CONNECTION = '/api/connection'
const SIGN_IN_POLL_MS = 450
const STATUS_POLL_MS = 15_000
const LAUNCH_PATH = /^\/api\/connection\/authorize\/[A-Za-z0-9_-]+$/

/** Show and manage the ChatGPT connection, and report the verified grading account. */
export function ConnectionSettings({ request, onGradingAccount, onConnectionRestored }: {
  request: LocalRequest
  onGradingAccount?: (account: GradingAccount) => void
  onConnectionRestored?: () => void
}) {
  const [lastStatus, setLastStatus] = useState<Status>()
  const [busy, setBusy] = useState(false)
  const [statusError, setStatusError] = useState<string>()
  const [actionError, setActionError] = useState<string>()
  const [launchPath, setLaunchPath] = useState('')
  const refreshStatus = useRef<(() => void) | null>(null)
  const errorText = useRef<HTMLParagraphElement>(null)

  const phase = lastStatus?.state
  const status = statusError === undefined ? lastStatus : undefined
  const gradingAccount = status && (status.plan_authorized && !status.reauthorization_required ? status.email : null)
  useEffect(() => { onGradingAccount?.(gradingAccount) }, [onGradingAccount, gradingAccount])
  useEffect(() => { if (actionError) errorText.current?.focus() }, [actionError])
  useEffect(() => {
    if (busy) return

    let activeRead: AbortController | undefined
    let timer: ReturnType<typeof setTimeout> | undefined
    function schedule(state: Status['state'] | undefined) {
      if (document.visibilityState !== 'hidden') {
        timer = setTimeout(readStatus, state === 'signing_in' ? SIGN_IN_POLL_MS : STATUS_POLL_MS)
      }
    }

    async function readStatus() {
      clearTimeout(timer)
      activeRead?.abort()
      if (document.visibilityState === 'hidden') return
      const controller = new AbortController()
      activeRead = controller
      let nextPhase = phase
      try {
        const next = await request<Status>(CONNECTION, { signal: controller.signal })
        if (controller.signal.aborted) return
        nextPhase = next.state
        setLastStatus(next)
        setStatusError(undefined)
        if (next.state !== 'signing_in') setLaunchPath('')
        if (phase === 'signing_in' && next.state === 'connected' && next.access_token_usable) {
          onConnectionRestored?.()
        }
      } catch (failure) {
        if (!controller.signal.aborted) {
          const detail = failure instanceof Error ? failure.message : ''
          setStatusError(detail || 'Could not load connection status.')
        }
      } finally {
        if (!controller.signal.aborted) schedule(nextPhase)
      }
    }

    const refresh = () => { void readStatus() }
    refreshStatus.current = refresh
    window.addEventListener('focus', refresh)
    document.addEventListener('visibilitychange', refresh)
    if (phase === undefined) refresh()
    else schedule(phase)

    return () => {
      activeRead?.abort()
      clearTimeout(timer)
      refreshStatus.current = null
      window.removeEventListener('focus', refresh)
      document.removeEventListener('visibilitychange', refresh)
    }
  }, [request, busy, phase, onConnectionRestored])

  async function signIn() {
    const popup = window.open('about:blank', '_blank')
    if (popup) popup.opener = null
    setBusy(true)
    setActionError(undefined)
    try {
      const reply = await request<{ launch_path: string }>(CONNECTION + '/sign-in', { method: 'POST' })
      if (!LAUNCH_PATH.test(reply.launch_path)) throw new Error('The local sign-in link was invalid.')
      setLastStatus({
        state: 'signing_in',
        email: status?.email ?? null,
        plan_authorized: status?.plan_authorized ?? false,
        access_token_usable: status?.access_token_usable ?? false,
        reauthorization_required: status?.reauthorization_required ?? false,
        message: null,
        remote_revocation_unconfirmed: status?.remote_revocation_unconfirmed ?? false,
        requires_consent: status?.requires_consent ?? false,
      })
      if (popup && !popup.closed) {
        try {
          popup.location.replace(reply.launch_path)
        } catch {
          setLaunchPath(reply.launch_path)
        }
      } else {
        setLaunchPath(reply.launch_path)
      }
    } catch (failure) {
      popup?.close()
      setActionError(failure instanceof Error ? failure.message : 'Could not start sign-in.')
    } finally {
      setBusy(false)
    }
  }

  async function finish(action: 'cancel' | 'sign-out') {
    setBusy(true)
    setActionError(undefined)
    try {
      setLastStatus(await request<Status>(CONNECTION + '/' + action, { method: 'POST' }))
      setLaunchPath('')
    } catch (failure) {
      setActionError(failure instanceof Error ? failure.message : 'Could not update the connection.')
    } finally {
      setBusy(false)
    }
  }

  const signingIn = status?.state === 'signing_in'
  const hasAccount = status?.email || status?.plan_authorized
  const shouldReconnect = status && (status.reauthorization_required || (status.plan_authorized && !status.access_token_usable))
  const error = statusError ?? actionError
  return (
    <section className="panel" aria-labelledby="chatgpt-heading">
      <h2 id="chatgpt-heading">ChatGPT connection</h2>
      <div className="settings-row connection-row">
        <div aria-live="polite">
          <p className={status?.state === 'connected' && status.access_token_usable ? 'state authorized' : 'state'}>
            {status ? LABELS[status.state] : statusError ? 'Status unavailable' : 'Checking connection…'}
          </p>
          {status?.email && <p>{status.email}</p>}
          {status?.plan_authorized && (
            <p className="muted">
              {status.access_token_usable ? 'ChatGPT plan use authorized' : 'ChatGPT plan permission saved'}
            </p>
          )}
          {signingIn && <p className="muted">Complete authorization in the ChatGPT tab.</p>}
          {status?.message && <p className="muted">{status.message}</p>}
        </div>
        <div className="actions">
          {signingIn ? (
            <>
              {launchPath && <a className="button" href={launchPath} target="_blank" rel="noreferrer">
                Open ChatGPT sign-in <ExternalLink size={15} aria-hidden="true" />
              </a>}
              <button disabled={busy} onClick={() => void finish('cancel')}>Cancel sign-in</button>
            </>
          ) : (
            <>
              {status && (!status.plan_authorized || shouldReconnect) && (
                <button className="chatgpt-button" data-oai-siwc-button="" disabled={busy} onClick={() => void signIn()}>
                  {status.requires_consent ? 'Enable ChatGPT plan use' :
                    shouldReconnect ? 'Reconnect ChatGPT' : 'Continue with ChatGPT'}
                </button>
              )}
              {hasAccount && <button disabled={busy} onClick={() => void finish('sign-out')}>Sign out</button>}
            </>
          )}
          {statusError && (
            <button disabled={busy} onClick={() => refreshStatus.current?.()}>Retry status</button>
          )}
        </div>
      </div>
      {error && <p ref={errorText} className="error" role="alert" tabIndex={-1}>{error}</p>}
      {status?.remote_revocation_unconfirmed && (
        <p className="notice" role="status">Remote revocation could not be confirmed. You can disconnect the app in ChatGPT.</p>
      )}
      <div className="privacy">
        <h3>Plan usage and privacy</h3>
        <p>Eligible grading requests will use your ChatGPT plan or credits if you allow them. Signing out keeps your local learning data. A grading request already sent may still finish.</p>
        <p>Other ChatGPT and Codex activity can share this allowance. Use your ChatGPT settings to set connected-app limits or turn off additional credit use.</p>
        <ul className="control-links">
          <li><a href="https://chatgpt.com/settings/usage" target="_blank" rel="noreferrer">Manage usage and credits <ExternalLink size={14} aria-hidden="true" /></a></li>
          <li><a href="https://help.openai.com/en/articles/7730893-data-controls-in-chatgpt" target="_blank" rel="noreferrer">Data controls <ExternalLink size={14} aria-hidden="true" /></a></li>
          <li><a href="https://help.openai.com/en/articles/20001542-using-your-chatgpt-plan-in-other-apps-and-sites#disconnect-an-app-or-site" target="_blank" rel="noreferrer">Disconnect in ChatGPT <ExternalLink size={14} aria-hidden="true" /></a></li>
        </ul>
      </div>
    </section>
  )
}
