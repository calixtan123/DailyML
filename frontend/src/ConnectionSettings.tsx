import { ExternalLink } from 'lucide-react'
import { useEffect, useState } from 'react'
import type { LocalRequest } from './api'

const LABELS = {
  signed_out: 'Signed out',
  signing_in: 'Signing in',
  connected: 'Connected',
  reconnect: 'Reconnect required',
  cancelled: 'Sign-in cancelled',
  timeout: 'Sign-in timed out',
  error: 'Sign-in failed',
} as const

type Status = {
  state: keyof typeof LABELS
  email: string | null
  plan_authorized: boolean
  message: string | null
  remote_revocation_unconfirmed: boolean
}

const CONNECTION = '/api/connection'
const POLL_INTERVAL_MS = 450
const LAUNCH_PATH = /^\/api\/connection\/authorize\/[A-Za-z0-9_-]+$/

export function ConnectionSettings({ request }: { request: LocalRequest }) {
  const [status, setStatus] = useState<Status>()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [launchPath, setLaunchPath] = useState('')
  const [statusVersion, setStatusVersion] = useState(0)

  const phase = status?.state
  const polling = phase === 'signing_in'
  useEffect(() => {
    if (busy || (phase !== undefined && phase !== 'signing_in')) return

    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    async function readStatus() {
      try {
        const next = await request<Status>(CONNECTION, { signal: controller.signal })
        if (controller.signal.aborted) return
        setStatus(next)
        setError('')
        if (next.state === 'signing_in' && polling) {
          timer = setTimeout(readStatus, POLL_INTERVAL_MS)
        } else if (next.state !== 'signing_in') {
          setLaunchPath('')
        }
      } catch (failure) {
        if (!controller.signal.aborted) {
          setError(failure instanceof Error ? failure.message : 'Could not load connection status.')
        }
      }
    }

    if (polling) timer = setTimeout(readStatus, POLL_INTERVAL_MS)
    else void readStatus()

    return () => {
      controller.abort()
      clearTimeout(timer)
    }
  }, [request, busy, phase, polling, statusVersion])

  async function signIn() {
    const popup = window.open('about:blank', '_blank')
    if (popup) popup.opener = null
    setBusy(true)
    setError('')
    try {
      const reply = await request<{ launch_path: string }>(CONNECTION + '/sign-in', { method: 'POST' })
      if (!LAUNCH_PATH.test(reply.launch_path)) throw new Error('The local sign-in link was invalid.')
      setStatus({
        state: 'signing_in',
        email: status?.email ?? null,
        plan_authorized: status?.plan_authorized ?? false,
        message: null,
        remote_revocation_unconfirmed: status?.remote_revocation_unconfirmed ?? false,
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
      setError(failure instanceof Error ? failure.message : 'Could not start sign-in.')
    } finally {
      setBusy(false)
    }
  }

  async function finish(action: 'cancel' | 'sign-out') {
    setBusy(true)
    setError('')
    try {
      setStatus(await request<Status>(CONNECTION + '/' + action, { method: 'POST' }))
      setLaunchPath('')
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : 'Could not update the connection.')
    } finally {
      setBusy(false)
    }
  }

  const signingIn = status?.state === 'signing_in'
  const hasAccount = status?.email || status?.plan_authorized
  return (
    <section className="panel" aria-labelledby="chatgpt-heading">
      <h2 id="chatgpt-heading">ChatGPT connection</h2>
      <div className="settings-row connection-row">
        <div aria-live="polite">
          <p className={status?.plan_authorized ? 'state authorized' : 'state'}>
            {status ? LABELS[status.state] : error ? 'Status unavailable' : 'Checking connection…'}
          </p>
          {status?.email && <p>{status.email}</p>}
          {status?.plan_authorized && <p className="muted">ChatGPT plan use authorized</p>}
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
              {status && status.state !== 'connected' && (
                <button className="chatgpt-button" data-oai-siwc-button="" disabled={busy} onClick={() => void signIn()}>
                  Continue with ChatGPT
                </button>
              )}
              {hasAccount && <button disabled={busy} onClick={() => void finish('sign-out')}>Sign out</button>}
            </>
          )}
          {error && (
            <button disabled={busy} onClick={() => {
              setStatus(undefined)
              setStatusVersion((version) => version + 1)
            }}>Retry status</button>
          )}
        </div>
      </div>
      {error && <p className="error" role="alert">{error}</p>}
      {status?.remote_revocation_unconfirmed && (
        <p className="notice" role="status">Remote revocation could not be confirmed. You can disconnect the app in ChatGPT.</p>
      )}
      <div className="privacy">
        <h3>Plan usage and privacy</h3>
        <p>When grading is available, it will send the question, marking guide, reference material and relevant answer or objection to OpenAI. This version only connects your account.</p>
        <p>Eligible grading requests will use your ChatGPT plan or credits if you allow them. Signing out keeps your local learning data. A grading request already sent may still finish.</p>
        <ul className="control-links">
          <li><a href="https://chatgpt.com/settings/usage" target="_blank" rel="noreferrer">Manage usage and credits <ExternalLink size={14} aria-hidden="true" /></a></li>
          <li><a href="https://help.openai.com/en/articles/7730893-data-controls-in-chatgpt" target="_blank" rel="noreferrer">Data controls <ExternalLink size={14} aria-hidden="true" /></a></li>
          <li><a href="https://help.openai.com/en/articles/20001542-using-your-chatgpt-plan-in-other-apps-and-sites#disconnect-an-app-or-site" target="_blank" rel="noreferrer">Disconnect in ChatGPT <ExternalLink size={14} aria-hidden="true" /></a></li>
        </ul>
      </div>
    </section>
  )
}
