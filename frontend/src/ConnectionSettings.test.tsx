import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ConnectionSettings } from './ConnectionSettings'
import type { LocalRequest } from './api'

const offline = {
  state: 'signed_out',
  email: null as string | null,
  requires_consent: false,
  plan_authorized: false,
  message: null,
  remote_revocation_unconfirmed: false,
}
const connected = { ...offline, state: 'connected', email: 'learner@example.test', plan_authorized: true }

function mockApi(initial = offline) {
  let current = initial
  const spy = vi.fn(async (path: string, _options?: RequestInit) => {
    if (path === '/api/connection') return current
    if (path === '/api/connection/sign-in') {
      current = { ...offline, state: 'signing_in' }
      return { launch_path: '/api/connection/authorize/mock-handle' }
    }
    if (path === '/api/connection/cancel') {
      current = { ...offline, state: 'cancelled' }
      return current
    }
    if (path === '/api/connection/sign-out') {
      current = { ...offline, remote_revocation_unconfirmed: true }
      return current
    }
    throw new Error('Unexpected route')
  })
  const request: LocalRequest = async <T,>(path: string, options?: RequestInit) =>
    await spy(path, options) as T
  return { request, spy, changeStatus: (status: typeof offline) => { current = status } }
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('ChatGPT connection controls', () => {
  it('opens only the one-use local URL and polls until verified sign-in completes', async () => {
    const replace = vi.fn()
    vi.spyOn(window, 'open').mockReturnValue({ location: { replace } } as unknown as Window)
    const api = mockApi()
    render(<ConnectionSettings request={api.request} />)
    await screen.findByText('Signed out')
    fireEvent.click(screen.getByRole('button', { name: 'Continue with ChatGPT' }))
    await screen.findByText('Signing in')
    expect(replace).toHaveBeenCalledWith('/api/connection/authorize/mock-handle')
    api.changeStatus(connected)
    await screen.findByText('Connected')
    expect(screen.getByText('learner@example.test')).toBeTruthy()
    expect(api.spy).toHaveBeenCalledWith('/api/connection/sign-in', { method: 'POST' })
  })

  it('offers a local link when popup is blocked and supports cancellation', async () => {
    vi.spyOn(window, 'open').mockReturnValue(null)
    const api = mockApi()
    render(<ConnectionSettings request={api.request} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Continue with ChatGPT' }))
    const link = await screen.findByRole('link', { name: 'Open ChatGPT sign-in' })
    expect(link.getAttribute('href')).toBe('/api/connection/authorize/mock-handle')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel sign-in' }))
    await screen.findByText('Sign-in cancelled')
    expect(screen.queryByRole('link', { name: 'Open ChatGPT sign-in' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Continue with ChatGPT' })).toBeTruthy()
  })

  it('allows retry following a callback timeout', async () => {
    vi.spyOn(window, 'open').mockReturnValue(null)
    const api = mockApi({ ...offline, state: 'timeout' })
    render(<ConnectionSettings request={api.request} />)
    await screen.findByText('Sign-in timed out')
    fireEvent.click(screen.getByRole('button', { name: 'Continue with ChatGPT' }))
    await screen.findByText('Signing in')
  })

  it('shows reconnect and exposes a remote revocation failure after local sign-out', async () => {
    const api = mockApi({ ...connected, state: 'reconnect', plan_authorized: false })
    render(<ConnectionSettings request={api.request} />)
    await screen.findByText('Reconnect required')
    fireEvent.click(screen.getByRole('button', { name: 'Sign out' }))
    await screen.findByText('Signed out')
    expect(screen.getByText(/Remote revocation could not be confirmed/)).toBeTruthy()
    expect(screen.getByRole('link', { name: 'Manage usage and credits' }).getAttribute('href'))
      .toBe('https://chatgpt.com/settings/usage')
  })

  it('notices expired authorization while Settings remains visible', async () => {
    vi.useFakeTimers()
    try {
      const api = mockApi(connected)
      render(<ConnectionSettings request={api.request} />)
      await act(async () => {})
      expect(screen.getByText('Connected')).toBeTruthy()
      api.changeStatus({ ...connected, state: 'reconnect', plan_authorized: false })
      await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
      expect(screen.getByText('Reconnect required')).toBeTruthy()
      expect(screen.queryByText('ChatGPT plan use authorized')).toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })

  it.each(['focus', 'visibilitychange'])('detects another tab signing out on %s', async (event) => {
    const api = mockApi(connected)
    render(<ConnectionSettings request={api.request} />)
    await screen.findByText('Connected')
    api.changeStatus(offline)
    const target = event === 'focus' ? window : document
    if (event === 'visibilitychange') {
      const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden')
      fireEvent(document, new Event(event))
      expect(screen.getByText('Connected')).toBeTruthy()
      visibility.mockReturnValue('visible')
    }
    fireEvent(target, new Event(event))
    await screen.findByText('Signed out')
    expect(screen.queryByText('ChatGPT plan use authorized')).toBeNull()
  })

  it('offers deliberate plan consent after the provider declines', async () => {
    vi.spyOn(window, 'open').mockReturnValue(null)
    const api = mockApi({ ...offline, state: 'declined', requires_consent: true })
    render(<ConnectionSettings request={api.request} />)
    await screen.findByText('Authorization declined')
    fireEvent.click(screen.getByRole('button', { name: 'Enable ChatGPT plan use' }))
    expect(await screen.findByText('Signing in')).toBeTruthy()
  })

  it('keeps a recoverable message when a connection request fails', async () => {
    const api = mockApi()
    api.spy.mockRejectedValueOnce(new Error('Reload the local app.'))
    render(<ConnectionSettings request={api.request} />)
    await screen.findByRole('alert')
    fireEvent.click(screen.getByRole('button', { name: 'Retry status' }))
    await screen.findByText('Signed out')
  })
})
