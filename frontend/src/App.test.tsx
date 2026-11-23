import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { App } from './App'

type Account = { email: string; model: string } | null
const reference = { question: 'Which split can we fit on?', answer: 'Training only.', sources: {} }

function stubLocalApi(account: Account = null, submitTest?: (options: RequestInit) => Promise<Response>) {
  let failStatusRead = false
  let draft = { text: '', version: 0 }
  let failNextDraftSave = false
  vi.stubGlobal('fetch', async (path: string, options?: RequestInit) => {
    switch (path) {
      case '/api/session':
        return Response.json({ csrf_token: 'local-test-session' })
      case '/api/practice/today':
        return Response.json({
          local_date: '2026-11-19', timezone: 'Europe/London', effective_timezone: 'Europe/London',
          goal: 2, answer_limit: 8000, shortage: null, unfinished: [],
          submitted: [], completion_counts: {}, first_use_date: '2026-11-19',
          selections: [{ kind: 'variety', item: {
            id: 'first', local_date: '2026-11-19', draft, submitted: false,
            question: {
              title: 'Generalisation', difficulty: 'introductory', section_title: 'Fundamentals',
              prompt: 'How should we evaluate new rentals?', objective: 'Explain generalisation.',
              expected_answer_words: [30, 80],
            },
          } }],
        })
      case '/api/practice/items/first/draft':
        if (options?.method !== 'PUT') throw new Error('Draft save requires PUT.')
        if (failNextDraftSave) {
          failNextDraftSave = false
          return Response.json({ detail: 'Database unavailable.' }, { status: 503 })
        }
        draft = { text: JSON.parse(String(options.body)).text, version: draft.version + 1 }
        return Response.json(draft)
      case '/api/connection/sign-out':
        if (options?.method !== 'POST') throw new Error('Sign-out requires POST.')
        account = null
        break
      case '/api/connection':
        if (failStatusRead) {
          failStatusRead = false
          throw new Error('Could not reach the local server.')
        }
        break
      case '/api/connection/models':
        if (!account) return Response.json({ detail: 'Connect ChatGPT first.' }, { status: 409 })
        return Response.json({
          models: [{ slug: account.model, display_name: account.model }],
          selected: account.model, version: 1, overridden: false, source: 'preferences',
        })
      case '/api/connection/test':
        if (options?.method === 'POST') {
          if (!submitTest) throw new Error('This flow should not submit a model test.')
          return submitTest(options)
        }
        return Response.json({ reference, latest: null })
      case '/api/usage':
        return Response.json({ attempts_24h: 0 })
      case '/api/settings':
        return Response.json({ timezone: 'Europe/London', version: 1, source: 'default',
          model_id: account?.model ?? null, model_source: account ? 'preferences' : 'default' })
      default:
        throw new Error(`Unexpected local request: ${path}`)
    }

    return Response.json({
      state: account ? 'connected' : 'signed_out',
      email: account?.email ?? null,
      plan_authorized: account !== null,
      access_token_usable: account !== null,
      reauthorization_required: false,
      remote_revocation_unconfirmed: false,
      requires_consent: false,
      message: null,
    })
  })
  return {
    changeAccount: (next: Account) => { account = next },
    failNextStatusRead: () => { failStatusRead = true },
    failDraftSave: () => { failNextDraftSave = true },
  }
}

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  window.history.replaceState(null, '', '/')
})

describe('Daily ML Practice', () => {
  it('opens Today for signed-out practice and keeps connection grading in Settings', async () => {
    stubLocalApi()
    render(<App />)

    expect(await screen.findByRole('heading', { name: 'Generalisation' })).toBeTruthy()
    expect(screen.queryByRole('heading', { name: 'Grading connection test' })).toBeNull()

    fireEvent.click(screen.getByRole('link', { name: 'Settings' }))
    expect(await screen.findByRole('heading', { name: 'Grading connection test' })).toBeTruthy()
    expect(await screen.findByText('Signed out')).toBeTruthy()
    expect(await screen.findByText('Which split can we fit on?')).toBeTruthy()
    expect(screen.queryByRole('combobox', { name: 'Grading model' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Run connection test' })).toBeNull()

    fireEvent.click(screen.getByRole('link', { name: 'Skip to content' }))
    expect(document.activeElement).toBe(screen.getByRole('main'))
    expect(window.location.hash).toBe('#/settings')

    fireEvent.click(screen.getByRole('link', { name: 'Today' }))
    expect(await screen.findByRole('heading', { name: 'Generalisation' })).toBeTruthy()
  })

  it.each(['back', 'forward'] as const)('keeps an unsaved edit after browser %s fails to save, then allows retry', async direction => {
    window.history.replaceState(null, '', '#/settings')
    const { failDraftSave } = stubLocalApi()
    render(<App />)
    fireEvent.click(screen.getByRole('link', { name: 'Today' }))
    if (direction === 'forward') {
      await screen.findByRole('textbox', { name: 'Your answer' })
      fireEvent.click(screen.getByRole('link', { name: 'Settings' }))
      await screen.findByRole('heading', { name: 'Settings' })
      window.history.back()
    }
    const editor = await screen.findByRole('textbox', { name: 'Your answer' })
    fireEvent.change(editor, { target: { value: 'Keep this answer across browser navigation.' } })
    failDraftSave()

    window.history[direction]()

    expect(await screen.findByRole('alert')).toBeTruthy()
    await waitFor(() => expect(window.location.hash).toBe('#/today'))
    expect(screen.getByRole('textbox', { name: 'Your answer' })).toHaveProperty('value',
      'Keep this answer across browser navigation.')
    expect(screen.getByRole('button', { name: 'Retry save' })).toBeTruthy()
    expect(screen.queryByRole('heading', { name: 'Settings' })).toBeNull()

    window.history[direction]()

    await screen.findByRole('heading', { name: 'Settings' })
    window.history[direction === 'back' ? 'forward' : 'back']()
    expect(await screen.findByRole('textbox', { name: 'Your answer' })).toHaveProperty('value',
      'Keep this answer across browser navigation.')
    expect(screen.getByText('Draft saved.')).toBeTruthy()
  })

  it('replaces grading controls for a different account and hides them on sign-out', async () => {
    window.history.replaceState(null, '', '#/settings')
    const firstAccount = { email: 'first@example.test', model: 'first-model' }
    const secondAccount = { email: 'second@example.test', model: 'second-model' }
    const { changeAccount } = stubLocalApi(firstAccount, async () => {
      throw new Error('The response was lost.')
    })
    render(<App />)

    expect(await screen.findByRole('option', { name: 'first-model', selected: true })).toBeTruthy()
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Run connection test' })).toHaveProperty('disabled', false)
    })
    fireEvent.click(screen.getByRole('button', { name: 'Run connection test' }))
    await screen.findByText('The response was lost.')
    expect(screen.getByRole('button', { name: 'Check previous test' })).toBeTruthy()

    changeAccount(secondAccount)
    fireEvent(window, new Event('focus'))
    expect(await screen.findByText('second@example.test')).toBeTruthy()
    expect(await screen.findByRole('option', { name: 'second-model', selected: true })).toBeTruthy()
    expect(screen.queryByRole('option', { name: 'first-model' })).toBeNull()

    changeAccount(firstAccount)
    fireEvent(window, new Event('focus'))
    await screen.findByRole('option', { name: 'first-model', selected: true })
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Run connection test' })).toHaveProperty('disabled', false)
    })
    fireEvent.click(screen.getByRole('button', { name: 'Run connection test' }))
    await screen.findByText('The response was lost.')

    fireEvent.click(screen.getByRole('button', { name: 'Sign out' }))
    expect(await screen.findByText('Signed out')).toBeTruthy()
    await waitFor(() => {
      expect(screen.queryByRole('combobox', { name: 'Grading model' })).toBeNull()
    })
    expect(screen.queryByRole('button', { name: 'Run connection test' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Check previous test' })).toBeNull()

    changeAccount(firstAccount)
    fireEvent(window, new Event('focus'))
    await screen.findByRole('option', { name: 'first-model', selected: true })
    expect(await screen.findByRole('button', { name: 'Run connection test' })).toBeTruthy()
  })

  it('recovers the same submitted grading request after returning to Settings', async () => {
    const submitted: { body: unknown; signal: RequestInit['signal'] }[] = []
    const { failNextStatusRead } = stubLocalApi({ email: 'learner@example.test', model: 'account-model' }, async options => {
      submitted.push({ body: JSON.parse(String(options.body)), signal: options.signal })
      if (submitted.length === 1) {
        return new Promise<Response>((_resolve, reject) => {
          options.signal?.addEventListener('abort', () => reject(options.signal?.reason), { once: true })
        })
      }
      return Response.json({
        id: 'original-request', model_id: 'account-model', state: 'needs_clarification', grade: null,
        error: null, reference, message: null,
        feedback: { status: 'needs_clarification', explanation: 'Which data split do you mean?' },
      })
    })
    window.history.replaceState(null, '', '#/settings')
    render(<App />)

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Run connection test' })).toHaveProperty('disabled', false)
    })
    fireEvent.click(screen.getByRole('button', { name: 'Run connection test' }))
    await waitFor(() => expect(submitted).toHaveLength(1))

    fireEvent.click(screen.getByRole('link', { name: 'Today' }))
    await screen.findByRole('heading', { name: 'Generalisation' })
    expect(submitted[0].signal?.aborted).toBe(true)
    failNextStatusRead()
    fireEvent.click(screen.getByRole('link', { name: 'Settings' }))
    await screen.findByText('Status unavailable')
    expect(screen.queryByRole('combobox', { name: 'Grading model' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Check previous test' })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Retry status' }))
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Check previous test' })).toHaveProperty('disabled', false)
    })
    fireEvent.click(screen.getByRole('button', { name: 'Check previous test' }))
    await screen.findByText(/Which data split do you mean\?/)
    expect(submitted.map(request => request.body)).toEqual([
      { idempotency_key: expect.any(String), model_id: 'account-model' },
      submitted[0].body,
    ])
    expect(screen.getByRole('button', { name: 'Run connection test' })).toHaveProperty('disabled', false)
  })
})
