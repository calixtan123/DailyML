import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { StrictMode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { App } from './App'
import type { SavedSubmission } from './PracticeFeedback'

const item = (id: string, title: string, text = '', version = 0, date = '2026-11-19') => ({
  id,
  local_date: date,
  question: {
    id, version: 1, title, prompt: `Explain ${title}.`, objective: 'Explain the main idea.',
    section_id: 'fundamentals', section_title: 'Fundamentals', concept_id: id,
    expected_answer_words: [30, 80], difficulty: 'introductory',
  },
  draft: { text, version },
  submitted: false,
  grading_status: null as string | null,
})

function practiceApi(
  save?: (edit: { text: string; version: number }) => Promise<Response>,
  resumed?: ReturnType<typeof item>,
  submissionApi?: (path: string, options?: RequestInit) => Response | Promise<Response>,
) {
  const first = item('first', 'Generalisation')
  const second = item('second', 'Data leakage')
  const third = item('third', 'Model monitoring')
  const today = {
    local_date: '2026-11-19', timezone: 'Europe/Paris', effective_timezone: 'Europe/London',
    goal: 2, answer_limit: 8000, shortage: null,
    selections: [first, second].map(item => ({ item, kind: 'variety' })),
    unfinished: resumed ? [resumed] : [],
    submitted: [] as ReturnType<typeof item>[],
    completion_counts: {} as Record<string, number>,
    first_use_date: '2026-11-19',
    reviews: {} as Record<string, { stage: number; due_date: string | null; awaiting_assessment: boolean }>,
    due_reviews: [] as { question_id: string; section_id: string; title: string }[],
  }
  const fetch = vi.fn(async (path: string, options?: RequestInit) => {
    if (path === '/api/session') return Response.json({ csrf_token: 'test' })
    if (path === '/api/practice/today') return Response.json(today)
    if (path === '/api/sections') {
      return Response.json([{ id: 'fundamentals', title: 'Fundamentals', question_count: 2 }])
    }
    if (path === '/api/questions?section_id=fundamentals') return Response.json([first.question])
    if (path === '/api/practice/items') {
      if (resumed) return Response.json({ today, item_id: resumed.id, message: null })
      today.selections.push({ item: third, kind: 'extra' })
      return Response.json({ today, item_id: 'third', message: null })
    }
    if (path === '/api/practice/items/first/draft' && options?.method === 'PUT') {
      const edit = JSON.parse(String(options.body))
      if (save) return save(edit)
      first.draft = { text: edit.text, version: edit.version + 1 }
      return Response.json(first.draft)
    }
    if (submissionApi) return submissionApi(path, options)
    throw new Error(`Unexpected request ${path}`)
  })
  vi.stubGlobal('fetch', fetch)
  return { fetch, today }
}

function practiceWithPendingReview(
  respond?: (path: string, options?: RequestInit) => Response | Promise<Response>,
) {
  let assessed = false
  const { today, fetch } = practiceApi(undefined, undefined, (path, options) => {
    if (path === '/api/practice/items/yesterday/submission') {
      const submission: SavedSubmission = {
        text: 'An answer from yesterday.', assessed_text: 'An answer from yesterday.', credited: true,
        completed_date: '2026-11-18', completed_timezone: 'Europe/London', clarification_draft: null,
        submission_version_id: 'yesterday-version', supported: false, versions: [], revision: null, revision_draft: null,
        request: {
          id: 'grading', state: assessed ? 'assessed' : 'in_flight', can_send: false,
          grade: assessed ? 3 : null, message: null, reference: { sources: {} },
          feedback: assessed ? {
            status: 'assessed', summary: 'Understood generalisation.', corrections: [],
            model_answer: 'Evaluate on unseen data.', reasoning: [], example: '', source_ids: [],
          } : null,
        },
      }
      return Response.json(submission)
    }
    if (respond) return respond(path, options)
    throw new Error(`Unexpected request ${path}`)
  })
  const pending = {
    ...item('yesterday', 'Generalisation', '', 0, '2026-11-18'),
    question: today.selections[0].item.question, submitted: true, grading_status: 'in_flight',
  }
  today.submitted.push(pending)
  today.reviews.first = { stage: 0, due_date: null, awaiting_assessment: true }
  return {
    today, fetch,
    assess() {
      today.reviews.first = { stage: 1, due_date: '2026-11-19', awaiting_assessment: false }
      today.due_reviews = [{ question_id: 'first', section_id: 'fundamentals', title: 'Generalisation' }]
      pending.grading_status = 'assessed'
      assessed = true
    },
  }
}

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  window.history.replaceState(null, '', '/')
})

describe('Today practice editor', () => {
  it('hides the retained editor until the requested history item loads', async () => {
    const target = item('12345678-1234-4321-8123-123456789abc', 'Saved history question', 'Saved history draft.', 2)
    let resolveItem: ((response: Response) => void) | undefined
    practiceApi(undefined, undefined, path => {
      if (path === `/api/practice/items/${target.id}`) {
        return new Promise(resolve => { resolveItem = resolve })
      }
      throw new Error(`Unexpected request ${path}`)
    })
    render(<App />)
    await screen.findByRole('textbox', { name: 'Your answer' })
    fireEvent.click(screen.getByRole('link', { name: 'Settings' }))
    await screen.findByRole('heading', { name: 'Settings' })

    await act(async () => {
      window.history.pushState(null, '', `#/today/${target.id}`)
      window.dispatchEvent(new HashChangeEvent('hashchange'))
    })
    await waitFor(() => expect(resolveItem).toBeDefined())
    expect(screen.queryByRole('textbox', { name: 'Your answer' })).toBeNull()

    await act(async () => resolveItem?.(Response.json(target)))
    expect(await screen.findByRole('textbox', { name: 'Your answer' }))
      .toHaveProperty('value', 'Saved history draft.')
  })

  it('reveals persisted help only after the server acknowledges it and keeps an edited answer on failure', async () => {
    let resolveHelp: ((response: Response) => void) | undefined
    const { fetch } = practiceApi(undefined, undefined, (path, options) => {
      if (path === '/api/practice/items/first/help/hint') {
        expect(options?.method).toBe('POST')
        return new Promise(resolve => { resolveHelp = resolve })
      }
      if (path === '/api/practice/items/first/help/explanation') return Response.json({ content: 'Stored explanation.' })
      throw new Error(`Unexpected request ${path}`)
    })
    render(<App />)
    const editor = await screen.findByRole('textbox', { name: 'Your answer' })
    fireEvent.change(editor, { target: { value: 'Keep this draft while requesting help.' } })

    fireEvent.click(screen.getByRole('button', { name: 'Show hint' }))
    await waitFor(() => expect(resolveHelp).toBeDefined())
    expect(screen.queryByText('Stored hint.')).toBeNull()
    await act(async () => resolveHelp?.(Response.json({ content: 'Stored hint.' })))
    expect(await screen.findByText('Stored hint.')).toBeTruthy()
    expect(editor).toHaveProperty('value', 'Keep this draft while requesting help.')

    fireEvent.click(screen.getByRole('button', { name: 'Show explanation' }))
    expect(await screen.findByText('Stored explanation.')).toBeTruthy()
    expect(fetch.mock.calls.filter(([path]) => String(path).includes('/help/'))).toHaveLength(2)
  })

  it('records an I do not know completion only after the displayed explanation is acknowledged', async () => {
    const acknowledgementKeys: string[] = []
    const gapSubmission = {
      text: "I don't know", assessed_text: "I don't know", assisted: false, credited: true,
      completed_date: '2026-11-19', completed_timezone: 'Europe/London', clarification_draft: null,
      request: { id: null, state: 'self_reported_gap', grade: null, can_send: false, message: null, feedback: null, reference: null },
    }
    const { today, fetch } = practiceApi(undefined, undefined, (path, options) => {
      if (path === '/api/practice/items/first/gap/reveal') {
        expect(options?.method).toBe('POST')
        return Response.json({ item_id: 'first', explanation: 'Read this reference explanation first.', acknowledgement_required: true })
      }
      if (path === '/api/practice/items/first/gap/acknowledge') {
        acknowledgementKeys.push(JSON.parse(String(options?.body)).idempotency_key)
        if (acknowledgementKeys.length === 1) return new Response(null, { status: 503 })
        today.completion_counts[today.local_date] = 1
        today.selections[0].item = { ...today.selections[0].item, submitted: true, grading_status: 'self_reported_gap' }
        return Response.json({ submission: gapSubmission, today })
      }
      if (path === '/api/practice/items/first/submission') return Response.json(gapSubmission)
      throw new Error(`Unexpected request ${path}`)
    })
    render(<App />)
    await screen.findByRole('textbox', { name: 'Your answer' })
    fireEvent.click(screen.getByRole('button', { name: "I don't know" }))
    expect(await screen.findByText('Read this reference explanation first.')).toBeTruthy()
    expect(screen.getByText('No practice yet today')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Mark as completed' })).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Mark as completed' }))
    expect(await screen.findByText('Could not record completion yet. The explanation remains available; try again.')).toBeTruthy()
    expect(screen.getByText('Read this reference explanation first.')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Mark as completed' }))
    expect(await screen.findByText('Self-reported gap')).toBeTruthy()
    expect(screen.getByText('1 of 2 completed today')).toBeTruthy()
    expect(screen.queryByText('Grading in progress')).toBeNull()
    expect(fetch.mock.calls.filter(([path]) => String(path).endsWith('/submit'))).toHaveLength(0)
    expect(acknowledgementKeys).toEqual([expect.any(String), acknowledgementKeys[0]])
  })

  it('does not reveal failed help and retains the edited answer', async () => {
    practiceApi(undefined, undefined, path => {
      if (path === '/api/practice/items/first/help/hint') return new Response(null, { status: 503 })
      throw new Error(`Unexpected request ${path}`)
    })
    render(<App />)
    const editor = await screen.findByRole('textbox', { name: 'Your answer' })
    fireEvent.change(editor, { target: { value: 'Keep this unsaved answer.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Show hint' }))

    expect(await screen.findByText('Could not save that hint. It has not been shown; try again.')).toBeTruthy()
    expect(screen.queryByRole('heading', { name: 'Hint' })).toBeNull()
    expect(editor).toHaveProperty('value', 'Keep this unsaved answer.')
  })

  it('opens the saved draft returned by exact question selection', async () => {
    const saved = item('older', 'Generalisation', 'My previous answer.', 1, '2026-11-18')
    practiceApi(undefined, saved)
    render(<App />)
    const editor = await screen.findByRole('textbox', { name: 'Your answer' })
    expect(editor).toHaveProperty('value', '')

    fireEvent.click(screen.getByText('Browse questions'))
    fireEvent.click(await screen.findByRole('button', { name: 'Practise this question' }))

    await waitFor(() => expect(editor).toHaveProperty('value', 'My previous answer.'))
  })

  it('shows a scheduled date and saves a Review sooner request from Today', async () => {
    const { today } = practiceApi(undefined, undefined, (path, options) => {
      expect(path).toBe('/api/practice/reviews/first/sooner')
      expect(options?.method).toBe('POST')
      expect(JSON.parse(String(options?.body)).idempotency_key).toEqual(expect.any(String))
      today.reviews.first = { stage: 2, due_date: '2026-11-20', awaiting_assessment: false }
      return Response.json(today.reviews.first)
    })
    today.reviews.first = { stage: 2, due_date: '2026-11-24', awaiting_assessment: false }
    render(<App />)
    const editor = await screen.findByRole('textbox', { name: 'Your answer' })
    expect(screen.getByText('2026-11-24', { selector: 'time' }).getAttribute('datetime')).toBe('2026-11-24')

    fireEvent.change(editor, { target: { value: 'My unsaved draft stays here.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Review sooner' }))

    expect((await screen.findByText('2026-11-20', { selector: 'time' })).getAttribute('datetime')).toBe('2026-11-20')
    expect(editor).toHaveProperty('value', 'My unsaved draft stays here.')
  })

  it('schedules tomorrow for a submitted concept without a date and retries the same request', async () => {
    const actionKeys: string[] = []
    const { today } = practiceApi(undefined, undefined, (path, options) => {
      if (path === '/api/practice/items/first/submission') return new Response(null, { status: 503 })
      expect(path).toBe('/api/practice/reviews/first/sooner')
      actionKeys.push(JSON.parse(String(options?.body)).idempotency_key)
      if (actionKeys.length === 1) return new Response(null, { status: 503 })
      today.reviews.first = { stage: 0, due_date: '2026-11-20', awaiting_assessment: true }
      return Response.json(today.reviews.first)
    })
    today.selections[0].item.submitted = true
    today.reviews.first = { stage: 0, due_date: null, awaiting_assessment: true }
    render(<App />)

    expect(await screen.findByText('Awaiting accepted assessment')).toBeTruthy()
    const sooner = screen.getByRole('button', { name: 'Review sooner' })
    fireEvent.click(sooner)
    await screen.findByText('Could not confirm the earlier review date. Try again.')
    fireEvent.click(sooner)

    expect(await screen.findByText('2026-11-20', { selector: 'time' })).toBeTruthy()
    expect(actionKeys).toEqual([expect.any(String), actionKeys[0]])
  })

  it('shows a newly due review when grading finishes during a Review sooner request', async () => {
    let respondToReview: (() => void) | undefined
    let assessed = false
    const submission = {
      text: 'An answer from yesterday.', assessed_text: 'An answer from yesterday.', credited: true,
      completed_date: '2026-11-18', completed_timezone: 'Europe/London', clarification_draft: null,
    }
    const { today } = practiceApi(undefined, undefined, path => {
      if (path === '/api/practice/reviews/first/sooner') {
        const response = Response.json({ stage: 0, due_date: '2026-11-20', awaiting_assessment: true })
        return new Promise(resolve => { respondToReview = () => resolve(response) })
      }
      if (path === '/api/practice/items/yesterday/submission') {
        return Response.json({
          ...submission,
          request: {
            id: 'grading', state: assessed ? 'assessed' : 'in_flight', can_send: false,
            grade: assessed ? 3 : null, message: null, reference: { sources: {} },
            feedback: assessed ? {
              status: 'assessed', summary: 'Understood generalisation.', corrections: [],
              model_answer: 'Evaluate on unseen data.', reasoning: [], example: '', source_ids: [],
            } : null,
          },
        })
      }
      throw new Error(`Unexpected request ${path}`)
    })
    today.submitted.push({
      ...item('yesterday', 'Generalisation', '', 0, '2026-11-18'),
      question: today.selections[0].item.question, submitted: true, grading_status: 'in_flight',
    })
    today.reviews.first = { stage: 0, due_date: null, awaiting_assessment: true }
    render(<StrictMode><App /></StrictMode>)
    fireEvent.click(await screen.findByRole('button', { name: /Generalisation.*Grading in progress/ }))
    await screen.findByText('Grading in progress', { selector: 'strong' })

    fireEvent.click(screen.getByRole('button', { name: 'Review sooner' }))
    await waitFor(() => expect(respondToReview).toBeDefined())
    today.reviews.first = { stage: 1, due_date: '2026-11-19', awaiting_assessment: false }
    today.due_reviews = [{ question_id: 'first', section_id: 'fundamentals', title: 'Generalisation' }]
    today.submitted[0].grading_status = 'assessed'
    assessed = true
    await screen.findByText('Grade 3 of 3')
    await act(async () => respondToReview?.())

    const question = screen.getByRole('article', { name: 'Generalisation' })
    expect((await within(question).findByText(/Review due/)).textContent).toBe('Review due 2026-11-19')
    expect(screen.getByRole('button', { name: 'Review Generalisation' })).toBeTruthy()
    expect(screen.getByRole('button', { name: /Generalisation.*Assessed/ }).getAttribute('aria-current')).toBe('true')
  })

  it.each(['fails', 'returns an older snapshot'])(
    'refreshes newly due reviews when More questions %s after grading finishes', async outcome => {
      let respondToAddition: (() => void) | undefined
      const { today, fetch, assess } = practiceWithPendingReview()
      const base = fetch.getMockImplementation()!
      fetch.mockImplementation(async (path, options) => {
        if (path !== '/api/practice/items') return base(path, options)
        const extra = item('third', 'Model monitoring')
        const response = outcome === 'fails'
          ? new Response(null, { status: 503 })
          : Response.json({
            today: { ...today, selections: [...today.selections, { item: extra, kind: 'extra' }] },
            item_id: extra.id, message: null,
          })
        if (outcome !== 'fails') today.selections.push({ item: extra, kind: 'extra' })
        return new Promise(resolve => { respondToAddition = () => resolve(response) })
      })
      render(<App />)
      fireEvent.click(await screen.findByRole('button', { name: /Generalisation.*Grading in progress/ }))
      await screen.findByText('Grading in progress', { selector: 'strong' })

      fireEvent.click(screen.getByRole('button', { name: 'More questions' }))
      await waitFor(() => expect(respondToAddition).toBeDefined())
      assess()
      await screen.findByText('Grade 3 of 3')
      await act(async () => respondToAddition?.())

      if (outcome === 'fails') {
        expect(screen.getByRole('alert').textContent).toBe('Could not open another question. Try again.')
      } else {
        expect(screen.getByRole('heading', { name: 'Model monitoring' })).toBeTruthy()
      }
      expect(await screen.findByRole('button', { name: 'Review Generalisation' })).toBeTruthy()
    },
  )

  it.each(['focus', 'timer'])(
    'checks pending grading on %s after switching questions and preserves an unsaved draft', async trigger => {
      vi.spyOn(Date, 'now').mockReturnValue(Date.parse('2026-11-19T12:00:00Z'))
      vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] })
      const { today, fetch, assess } = practiceWithPendingReview()
      today.selections[0].item = today.submitted.shift()!
      render(<App />)
      await screen.findByText('Grading in progress', { selector: 'strong' })
      fireEvent.click(screen.getByRole('button', { name: 'More questions' }))
      const draft = await screen.findByRole('textbox', { name: 'Your answer' })
      fireEvent.change(draft, { target: { value: 'Unsaved answer on the second question.' } })
      assess()
      expect(screen.queryByRole('button', { name: 'Review Generalisation' })).toBeNull()

      await act(async () => {
        if (trigger === 'focus') fireEvent(window, new Event('focus'))
        else await vi.advanceTimersByTimeAsync(60_000)
      })

      expect(await screen.findByRole('button', { name: 'Review Generalisation' })).toBeTruthy()
      expect(screen.getByRole('heading', { name: 'Model monitoring' })).toBeTruthy()
      expect(draft).toHaveProperty('value', 'Unsaved answer on the second question.')
      expect(fetch.mock.calls.some(([path]) => path.endsWith('/draft'))).toBe(false)
      fireEvent.click(screen.getByText('Browse questions'))
      const question = await screen.findByRole('article', { name: 'Generalisation' })
      expect(within(question).getByText(/Review due/).textContent).toBe('Review due 2026-11-19')
    },
  )

  it('opens an actionable due review even when the saved first recommendation is different', async () => {
    const { today, fetch } = practiceApi()
    today.reviews.third = { stage: 2, due_date: '2026-11-18', awaiting_assessment: false }
    today.due_reviews = [{ question_id: 'third', section_id: 'fundamentals', title: 'Model monitoring' }]
    render(<App />)
    await screen.findByRole('heading', { name: 'Generalisation' })

    fireEvent.click(screen.getByRole('button', { name: 'Review Model monitoring' }))

    await screen.findByRole('heading', { name: 'Model monitoring' })
    expect(fetch.mock.calls.filter(([path]) => path === '/api/practice/items')).toHaveLength(1)
    expect(JSON.parse(String(fetch.mock.calls.find(([path]) => path === '/api/practice/items')?.[1]?.body)))
      .toEqual({ section_id: 'fundamentals', question_id: 'third' })
    expect(screen.getByRole('button', { name: /Generalisation.*Recommendation/ })).toBeTruthy()
  })

  it('shows a due review date when browsing a section', async () => {
    const { today } = practiceApi()
    today.reviews.first = { stage: 1, due_date: '2026-11-18', awaiting_assessment: false }
    render(<App />)
    await screen.findByRole('textbox', { name: 'Your answer' })
    fireEvent.click(screen.getByText('Browse questions'))

    await screen.findByRole('button', { name: 'Practise this question' })
    const question = screen.getByRole('button', { name: 'Practise this question' }).closest('article')!
    expect(within(question).getByText('2026-11-18', { selector: 'time' })).toBeTruthy()
    expect(within(question).getByText(/Review due/)).toBeTruthy()
  })

  it('waits for SQLite acknowledgement before leaving an edited question', async () => {
    let acknowledge!: (response: Response) => void
    const { fetch } = practiceApi(() => new Promise(resolve => { acknowledge = resolve }))
    render(<App />)
    const editor = await screen.findByRole('textbox', { name: 'Your answer' })
    fireEvent.change(editor, { target: { value: 'My unsaved explanation.' } })
    const unload = new Event('beforeunload', { cancelable: true })
    expect(window.dispatchEvent(unload)).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: /Data leakage/ }))
    await waitFor(() => expect(fetch.mock.calls.some(([path]) => path.endsWith('/draft'))).toBe(true))
    expect((editor as HTMLTextAreaElement).value).toBe('My unsaved explanation.')
    expect(screen.queryByText('Draft saved.')).toBeNull()
    acknowledge(Response.json({ text: 'My unsaved explanation.', version: 1 }))
    await screen.findByRole('heading', { name: 'Data leakage' })
    expect(window.dispatchEvent(new Event('beforeunload', { cancelable: true }))).toBe(true)
  })

  it('keeps the current question and text when navigation save fails, then retries', async () => {
    let attempt = 0
    practiceApi(async (edit) => {
      attempt += 1
      if (attempt === 1) return Response.json({ detail: 'Database unavailable.' }, { status: 503 })
      return Response.json({ text: edit.text, version: 1 })
    })
    render(<App />)
    const editor = await screen.findByRole('textbox', { name: 'Your answer' })
    fireEvent.change(editor, { target: { value: 'Keep this exact edit.' } })
    fireEvent.click(screen.getByRole('button', { name: 'More questions' }))
    await screen.findByRole('alert')
    expect((editor as HTMLTextAreaElement).value).toBe('Keep this exact edit.')
    expect(screen.getByRole('heading', { name: 'Generalisation' })).toBeTruthy()
    expect(screen.queryByRole('heading', { name: 'Model monitoring' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Copy my answer' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Retry save' }))
    expect(await screen.findByText('Draft saved.')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'More questions' }))
    await screen.findByRole('heading', { name: 'Model monitoring' })
  })

  it('shows both versions on conflict and requires an explicit choice before saving local text', async () => {
    const { fetch } = practiceApi(async (edit) => edit.version === 0
      ? Response.json({
          detail: 'This draft changed in another tab.', saved: { text: 'Already saved in another tab.', version: 1 },
        }, { status: 409 })
      : Response.json({ text: edit.text, version: 2 }))
    render(<App />)
    const editor = await screen.findByRole('textbox', { name: 'Your answer' })
    fireEvent.change(editor, { target: { value: 'Local alternative.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save draft' }))
    const saved = await screen.findByRole('textbox', { name: 'Saved in another tab' })
    expect((editor as HTMLTextAreaElement).value).toBe('Local alternative.')
    expect((saved as HTMLTextAreaElement).value).toBe('Already saved in another tab.')
    expect(fetch.mock.calls.filter(([path]) => path.endsWith('/draft'))).toHaveLength(1)
    fireEvent.click(screen.getByRole('button', { name: 'Save my version' }))
    await screen.findByText('Draft saved.')
    expect((editor as HTMLTextAreaElement).value).toBe('Local alternative.')
  })

  it('allows 8,000 Unicode code points and retains the full editor text at 8,001', async () => {
    vi.spyOn(Date, 'now').mockReturnValue(Date.parse('2026-11-19T12:00:00Z'))
    const { fetch } = practiceApi()
    render(<App />)
    const editor = await screen.findByRole('textbox', { name: 'Your answer' })
    const maximum = '🧪'.repeat(8000)
    fireEvent.change(editor, { target: { value: maximum + 'x' } })
    expect(screen.getByText(/8,001.*8,000/)).toBeTruthy()
    expect((editor as HTMLTextAreaElement).value).toBe(maximum + 'x')
    expect(screen.getByRole('button', { name: 'Save draft' })).toHaveProperty('disabled', true)
    expect(fetch.mock.calls.some(([path]) => path.endsWith('/draft'))).toBe(false)
    fireEvent.change(editor, { target: { value: maximum } })
    fireEvent.click(screen.getByRole('button', { name: 'Save draft' }))
    await screen.findByText('Draft saved.')
  })

  it('loads the new day on rollover without replacing the active unsaved editor', async () => {
    const clock = vi.spyOn(Date, 'now').mockReturnValue(Date.parse('2026-11-19T23:59:59Z'))
    const { today } = practiceApi()
    render(<App />)
    const editor = await screen.findByRole('textbox', { name: 'Your answer' })
    fireEvent.change(editor, { target: { value: 'Still editing after midnight.' } })
    today.local_date = '2026-11-20'
    today.selections = [{ kind: 'variety', item: item('third', 'Model monitoring', '', 0, '2026-11-20') }]
    clock.mockReturnValue(Date.parse('2026-11-20T00:00:04Z'))
    await act(async () => { fireEvent(window, new Event('focus')) })
    expect(await screen.findByRole('button', { name: /Model monitoring/ })).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'Generalisation' })).toBeTruthy()
    expect((editor as HTMLTextAreaElement).value).toBe('Still editing after midnight.')
  })

  it('shows what OpenAI receives and describes the submit button with that notice', async () => {
    practiceApi()
    render(<App />)
    const description = 'For grading, OpenAI receives your answer, the question, its marking guide and reference material.'
    const editor = await screen.findByRole('textbox', { name: 'Your answer' })
    fireEvent.change(editor, { target: { value: 'My answer for grading.' } })

    expect(screen.getByText(description)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Submit answer', description })).toHaveProperty('disabled', false)
  })

  it('shows a calendar legend for all completion states in a week with no practice', async () => {
    practiceApi()
    render(<App />)

    const calendar = await screen.findByRole('region', { name: 'Weekly practice calendar' })
    expect(within(calendar).getByText('0 No practice')).toBeTruthy()
    expect(within(calendar).getByText('1 Partial')).toBeTruthy()
    expect(within(calendar).getByText('2+ Goal met')).toBeTruthy()
    expect(within(calendar).getByLabelText(/Thursday, 19 November 2026, 0 completed, No practice/)).toBeTruthy()
  })

  it('credits a saved submission before grading and keeps the other question usable', async () => {
    const original = 'An original answer with a useful example.'
    const submission = {
      text: original, assessed_text: original, credited: true,
      completed_date: '2026-11-19', completed_timezone: 'Europe/London',
      request: { id: 'grading', state: 'awaiting_access', feedback: null, message: null },
    }
    const { fetch, today } = practiceApi(undefined, undefined, (path, options) => {
      if (path.endsWith('/submit')) {
        expect(JSON.parse(String(options?.body))).toMatchObject({ text: original, version: 0 })
        today.completion_counts[today.local_date] = 1
        today.selections[0].item = { ...today.selections[0].item, submitted: true }
        return Response.json({ today, submission })
      }
      if (path.endsWith('/submission')) return Response.json(submission)
      throw new Error(`Unexpected request ${path}`)
    })
    render(<App />)
    const editor = await screen.findByRole('textbox', { name: 'Your answer' })
    expect(screen.getByText('No practice yet today')).toBeTruthy()
    fireEvent.change(editor, { target: { value: original } })
    fireEvent.click(screen.getByRole('button', { name: 'Submit answer' }))
    expect(await screen.findByText('Waiting for grading access')).toBeTruthy()
    expect(screen.getByText('1 of 2 completed today')).toBeTruthy()
    expect(screen.getByText(original)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: /Data leakage/ }))
    expect(await screen.findByRole('textbox', { name: 'Your answer' })).toHaveProperty('value', '')
    fireEvent.click(screen.getByRole('button', { name: /Generalisation/ }))
    expect(await screen.findByText('Waiting for grading access')).toBeTruthy()
    expect(fetch.mock.calls.filter(([path]) => path.endsWith('/submit'))).toHaveLength(1)
  })

  it('shows stored feedback without retrying and preserves counts above the daily goal', async () => {
    const { today, fetch } = practiceApi(undefined, undefined, path => {
      if (!path.endsWith('/submission')) throw new Error(`Unexpected request ${path}`)
      return Response.json({
        text: 'Stored original', assessed_text: 'Stored original', credited: true, assisted: true,
        completed_date: '2026-11-19', completed_timezone: 'Europe/London',
        request: {
          id: 'assessed', state: 'assessed', grade: 3,
          reference: { sources: { reading: 'https://example.org/' } },
          feedback: {
            status: 'assessed', summary: 'Tracked evaluation correctly.', corrections: ['State the split.'],
            model_answer: 'Use a held-out dataset.', reasoning: ['Make the split explicit.'],
            example: 'A test set is held back.', source_ids: ['reading'],
          },
        },
      })
    })
    today.first_use_date = '2026-11-17'
    today.completion_counts['2026-11-19'] = 3
    today.selections[0].item = { ...today.selections[0].item, submitted: true }
    render(<App />)
    expect(await screen.findByText('Grade 3 of 3')).toBeTruthy()
    expect(screen.getByText('3 completed today · Goal met')).toBeTruthy()
    expect(screen.getByLabelText(/Monday, 16 November 2026, Before first use/)).toBeTruthy()
    expect(screen.getByLabelText(/Tuesday, 17 November 2026, 0 completed, No practice/)).toBeTruthy()
    expect(screen.getByLabelText(/Friday, 20 November 2026, Future date/)).toBeTruthy()
    expect(screen.getByText('AI-generated feedback')).toBeTruthy()
    expect(screen.getByText('Assisted practice — help was viewed before this answer was submitted.')).toBeTruthy()
    expect(screen.getByText('State the split.')).toBeTruthy()
    expect(screen.getByRole('link', { name: 'reading', hidden: true })).toHaveProperty('href', 'https://example.org/')
    expect(fetch.mock.calls.filter(([path]) => path.endsWith('/retry') || path.endsWith('/submit'))).toHaveLength(0)
  })
})

type TestDraft = { text: string; version: number }
const originalClarifiedAnswer = 'Keep separate data.'
const clarificationRequest: Exclude<SavedSubmission['request'], { state: 'self_reported_gap' }> = { id: 'clarification', state: 'needs_clarification',
  feedback: { status: 'needs_clarification', explanation: 'What was held out?' },
  message: null, grade: null, can_send: false, reference: { sources: {} },
}

function clarificationApi(
  save?: (edit: TestDraft) => Response,
  read?: (result: Response) => Response | Promise<Response>,
) {
  let draft: TestDraft | null = { text: originalClarifiedAnswer, version: 0 }
  let request: Exclude<SavedSubmission['request'], { state: 'self_reported_gap' }> = clarificationRequest
  let assessedText = originalClarifiedAnswer
  const result = () => Response.json({
    text: originalClarifiedAnswer, assessed_text: assessedText, credited: true,
    completed_date: '2026-11-19', completed_timezone: 'Europe/London',
    request, clarification_draft: draft,
  })
  const api = practiceApi(undefined, undefined, (path, options) => {
    if (path === '/api/practice/items/first/submission') return read ? read(result()) : result()
    if (path === '/api/practice/items/first/clarification-draft' && options?.method === 'PUT') {
      const edit = JSON.parse(String(options.body))
      expect(edit.request_id).toBe('clarification')
      if (save) return save(edit)
      if (!draft || edit.version !== draft.version) return Response.json({ saved: draft }, { status: 409 })
      draft = { text: edit.text, version: draft.version + 1 }
      return Response.json(draft)
    }
    if (path === '/api/practice/items/first/retry' && options?.method === 'POST') {
      const edit = JSON.parse(String(options.body))
      expect(edit.request_id).toBe('clarification')
      expect({ text: edit.text, version: edit.version }).toEqual(draft)
      assessedText = edit.text
      draft = null
      request = { ...request, id: 'continued', state: 'in_flight', feedback: null }
      return result()
    }
    throw new Error(`Unexpected request ${path}`)
  })
  const first = api.today.selections[0].item
  api.today.selections[0].item = { ...first, submitted: true, grading_status: 'needs_clarification' }
  return api
}

describe('Clarification draft', () => {
  it('warns before unloading with unsaved changes and restores an explicit save after refresh', async () => {
    clarificationApi()
    const { unmount } = render(<App />)
    const editor = await screen.findByRole('textbox', { name: 'Clarified answer' })
    const text = 'Hold back a separately collected test set.'
    fireEvent.change(editor, { target: { value: text } })
    expect(window.dispatchEvent(new Event('beforeunload', { cancelable: true }))).toBe(false)
    await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Save draft' })) })
    expect(screen.getByText('Draft saved.')).toBeTruthy()
    expect(window.dispatchEvent(new Event('beforeunload', { cancelable: true }))).toBe(true)

    unmount()
    render(<App />)
    expect(await screen.findByRole('textbox', { name: 'Clarified answer' })).toHaveProperty('value', text)
    expect(screen.getByText(originalClarifiedAnswer)).toBeTruthy()
  })

  it('saves clarification before switching away and reloads it when the learner returns', async () => {
    clarificationApi()
    render(<App />)
    const editor = await screen.findByRole('textbox', { name: 'Clarified answer' })
    const text = 'Reserve data for a single final evaluation.'
    fireEvent.change(editor, { target: { value: text } })

    fireEvent.click(screen.getByRole('button', { name: /Data leakage/ }))
    await screen.findByRole('heading', { name: 'Data leakage' })
    const returnToClarification = screen.getByRole('button', { name: /Generalisation/ })
    await waitFor(() => expect(returnToClarification).toHaveProperty('disabled', false))
    fireEvent.click(returnToClarification)

    expect(await screen.findByRole('textbox', { name: 'Clarified answer' })).toHaveProperty('value', text)
  })

  it('keeps clarification read-only until More questions finishes and unlocks it after a failed request', async () => {
    let respond: ((response: Response) => void) | undefined
    const { fetch } = clarificationApi()
    const practiceRequest = fetch.getMockImplementation()!
    fetch.mockImplementation((path, options) => {
      if (path !== '/api/practice/items') return practiceRequest(path, options)
      return new Promise(resolve => { respond = resolve })
    })
    render(<App />)
    const editor = await screen.findByRole('textbox', { name: 'Clarified answer' })
    const text = 'Hold out data for the final evaluation.'
    fireEvent.change(editor, { target: { value: text } })
    fireEvent.click(screen.getByRole('button', { name: 'More questions' }))
    await screen.findByText('Draft saved.')

    expect(respond).toBeDefined()
    expect(editor).toHaveProperty('readOnly', true)
    expect(screen.getByRole('button', { name: 'Send clarified answer' })).toHaveProperty('disabled', true)
    fireEvent.change(editor, { target: { value: 'A late input event should be ignored.' } })
    expect(editor).toHaveProperty('value', text)
    await act(async () => respond?.(Response.json({ detail: 'Database unavailable.' }, { status: 503 })))
    await screen.findByText('Could not open another question. Try again.')
    expect(editor).toHaveProperty('readOnly', false)
    expect(editor).toHaveProperty('value', text)

    const revised = 'Keep a separate test set until model selection is finished.'
    fireEvent.change(editor, { target: { value: revised } })
    respond = undefined
    fireEvent.click(screen.getByRole('button', { name: 'More questions' }))
    await waitFor(() => expect(respond).toBeDefined())
    expect(editor).toHaveProperty('readOnly', true)
    await act(async () => respond?.(await practiceRequest('/api/practice/items')))
    await screen.findByRole('heading', { name: 'Model monitoring' })
    fireEvent.click(screen.getByRole('button', { name: /Generalisation/ }))
    expect(await screen.findByRole('textbox', { name: 'Clarified answer' })).toHaveProperty('value', revised)
  })

  it('keeps a clarification and focus on the failed save when leaving Today', async () => {
    let failures = true
    clarificationApi(edit => failures
      ? Response.json({ detail: 'Database unavailable.' }, { status: 503 })
      : Response.json({ text: edit.text, version: 1 }))
    render(<App />)
    const editor = await screen.findByRole('textbox', { name: 'Clarified answer' })
    const text = 'Keep editing until the database is reachable.'
    fireEvent.change(editor, { target: { value: text } })

    fireEvent.click(screen.getByRole('link', { name: 'Settings' }))
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('Could not save your clarification draft.')
    await waitFor(() => expect(document.activeElement).toBe(alert))
    expect(editor).toHaveProperty('value', text)
    expect(screen.queryByRole('heading', { name: 'Settings' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Copy my answer' })).toBeTruthy()

    failures = false
    fireEvent.click(screen.getByRole('button', { name: 'Retry save' }))
    await screen.findByText('Draft saved.')
    expect(editor).toHaveProperty('value', text)
  })

  it('preserves an acknowledged clarification when an older status read arrives after it', async () => {
    let pendingRead: (() => void) | undefined
    let reads = 0
    clarificationApi(undefined, result => {
      reads += 1
      if (reads !== 2) return result
      return new Promise(resolve => { pendingRead = () => resolve(result) })
    })
    render(<App />)
    await screen.findByRole('textbox', { name: 'Clarified answer' })
    fireEvent.click(screen.getByRole('link', { name: 'Settings' }))
    await screen.findByRole('heading', { name: 'Settings' })
    fireEvent.click(screen.getByRole('link', { name: 'Today' }))
    await waitFor(() => expect(pendingRead).toBeDefined())
    const editor = screen.getByRole('textbox', { name: 'Clarified answer' })
    const text = 'Keep the latest acknowledged clarification.'
    fireEvent.change(editor, { target: { value: text } })
    fireEvent.click(screen.getByRole('button', { name: 'Save draft' }))
    await screen.findByText('Draft saved.')

    await act(async () => { pendingRead?.() })

    expect(editor).toHaveProperty('value', text)
  })

  it('keeps unsaved text available to copy when a reopened status read shows another tab sent the clarification', async () => {
    let finishReopen: (() => void) | undefined
    let reads = 0
    clarificationApi(undefined, async result => {
      reads += 1
      if (reads === 1) return result
      const saved: SavedSubmission = await result.json()
      const continued = Response.json({
        ...saved,
        assessed_text: 'The other tab sent this clarification.',
        request: { ...saved.request, id: 'continued', state: 'in_flight', feedback: null },
        clarification_draft: null,
      })
      if (reads > 2) return continued
      return new Promise(resolve => { finishReopen = () => resolve(continued) })
    })
    render(<App />)
    await screen.findByRole('textbox', { name: 'Clarified answer' })
    fireEvent.click(screen.getByRole('link', { name: 'Settings' }))
    await screen.findByRole('heading', { name: 'Settings' })
    fireEvent.click(screen.getByRole('link', { name: 'Today' }))
    await waitFor(() => expect(finishReopen).toBeDefined())
    const text = 'Keep this exact unsaved clarification from this tab.'
    fireEvent.change(screen.getByRole('textbox', { name: 'Clarified answer' }), { target: { value: text } })
    await act(async () => finishReopen?.())

    const editor = screen.getByRole('textbox', { name: 'Clarified answer' })
    expect(editor).toHaveProperty('value', text)
    expect(editor).toHaveProperty('readOnly', true)
    expect(document.activeElement).toBe(screen.getByRole('alert'))
    expect(window.dispatchEvent(new Event('beforeunload', { cancelable: true }))).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: 'Copy my answer' }))
    await screen.findByText(/Your answer is selected/)
    expect(editor).toHaveProperty('selectionStart', 0)
    expect(editor).toHaveProperty('selectionEnd', text.length)

    const otherQuestion = screen.getByRole('button', { name: /Data leakage/ })
    await act(async () => { fireEvent.click(otherQuestion) })
    expect(screen.getByRole('heading', { name: 'Generalisation' })).toBeTruthy()
    expect(editor).toHaveProperty('value', text)

    fireEvent.click(screen.getByRole('button', { name: 'Discard edit and refresh grading status' }))
    await screen.findByRole('button', { name: /Generalisation.*Grading in progress/ })
    expect(screen.queryByRole('textbox', { name: 'Clarified answer' })).toBeNull()
    expect(screen.getByText('The other tab sent this clarification.')).toBeTruthy()
    expect(window.dispatchEvent(new Event('beforeunload', { cancelable: true }))).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: /Data leakage/ }))
    await screen.findByRole('heading', { name: 'Data leakage' })
  })

  it('saves the latest clarification before sending it and updates the selected grading label', async () => {
    clarificationApi()
    render(<App />)
    const editor = await screen.findByRole('textbox', { name: 'Clarified answer' })
    fireEvent.change(editor, { target: { value: 'Use a held-out test set only after selecting the model.' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send clarified answer' }))

    await screen.findByRole('button', { name: /Generalisation.*Grading in progress/ })
    expect(screen.queryByRole('textbox', { name: 'Clarified answer' })).toBeNull()
    expect(screen.getByText(originalClarifiedAnswer)).toBeTruthy()
  })

  it('shows both versions on a clarification save conflict and allows saving the local alternative', async () => {
    clarificationApi(edit => edit.version === 0
      ? Response.json({ detail: 'This draft changed in another tab.', saved: {
        text: 'The newer draft stored by another tab.', version: 1,
      } }, { status: 409 })
      : Response.json({ text: edit.text, version: 2 }))
    render(<App />)
    const editor = await screen.findByRole('textbox', { name: 'Clarified answer' })
    const text = 'Keep this distinct local clarification.'
    fireEvent.change(editor, { target: { value: text } })

    fireEvent.click(screen.getByRole('button', { name: /Data leakage/ }))
    expect(await screen.findByRole('textbox', { name: 'Saved in another tab' }))
      .toHaveProperty('value', 'The newer draft stored by another tab.')
    expect(editor).toHaveProperty('value', text)
    expect(screen.getByRole('heading', { name: 'Generalisation' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Save my version' }))
    await screen.findByText('Draft saved.')
    expect(editor).toHaveProperty('value', text)
  })
})

describe('Practice session grading summaries', () => {
  it('shows distinct grading states for selected and previous submissions in both selectors', async () => {
    const { today, fetch } = clarificationApi()
    today.submitted.push({
      ...item('previous', 'Model monitoring', '', 0, '2026-11-18'),
      submitted: true, grading_status: 'failed_recoverable',
    })
    render(<App />)

    expect(await screen.findByRole('button', { name: /Generalisation.*Needs clarification/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: /Model monitoring.*2026-11-18.*Needs retry/ })).toBeTruthy()
    const select = screen.getByRole('combobox', { name: 'Session question' })
    expect(within(select).getByRole('option', { name: /Generalisation.*Needs clarification/ })).toBeTruthy()
    expect(within(select).getByRole('option', { name: /Model monitoring.*2026-11-18.*Needs retry/ })).toBeTruthy()
    expect(fetch.mock.calls.some(([path]) => path === '/api/practice/items/previous/submission')).toBe(false)
  })
})

const assessedOriginal: NonNullable<SavedSubmission['revision']>['request'] = {
  id: 'original-assessment', state: 'assessed', grade: 2, can_send: false, message: null,
  reference: { sources: {} },
  feedback: {
    status: 'assessed', summary: 'Explain when the test set is used.', corrections: [],
    model_answer: 'Evaluate on new data after model selection.', reasoning: [], example: '', source_ids: [],
  },
}

function revisionApi(outcome: 'outcome_unknown' | 'conflict' | 'needs_clarification' = 'outcome_unknown') {
  const original = 'Evaluate the model on data it has never seen.'
  const originalDraft = { text: original, version: 0 }
  let draft: TestDraft | null = originalDraft
  let firstRevisedText: string | undefined
  let revision: {
    assessed_text: string
    clarification_draft: TestDraft | null
    request: NonNullable<SavedSubmission['revision']>['request']
    submission_version_id: string
    supported: boolean
  } | null = null
  function result() {
    return Response.json({
      text: original, assessed_text: original, credited: true,
      completed_date: '2026-11-11', completed_timezone: 'Europe/London',
      request: assessedOriginal, clarification_draft: null, submission_version_id: 'original',
      supported: false, versions: firstRevisedText ? [{
        id: 'first-revision', kind: 'revision', role: 'revision', text: firstRevisedText, supported: true,
        parent_version_id: 'original', created_at: 1, request: revision?.request,
      }] : [], revision, revision_draft: draft,
    })
  }
  const { today, fetch } = practiceApi(undefined, undefined, (path, options) => {
    if (path.endsWith('/previous/submission')) return result()
    const edit = JSON.parse(String(options?.body))
    if (path.endsWith('/revision-draft')) {
      expect(edit.request_id).toBe('original-assessment')
      expect(edit.version).toBe(draft?.version)
      draft = { text: edit.text, version: edit.version + 1 }
      return Response.json(draft)
    }
    if (path.endsWith('/revise')) {
      expect(edit.request_id).toBe('original-assessment')
      expect({ text: edit.text, version: edit.version }).toEqual(draft)
      revision = {
        assessed_text: outcome === 'conflict' ? 'The answer submitted in the other tab.' : edit.text,
        clarification_draft: outcome === 'needs_clarification' ? { text: edit.text, version: 0 } : null,
        submission_version_id: 'first-revision', supported: true,
        request: outcome === 'needs_clarification'
          ? { ...clarificationRequest, id: 'revision-assessment' }
          : { ...assessedOriginal, id: 'revision-assessment', state: 'outcome_unknown', grade: null, feedback: null },
      }
      firstRevisedText = revision.assessed_text
      draft = null
      return outcome === 'conflict'
        ? Response.json({ detail: 'The revision was already submitted.' }, { status: 409 })
        : result()
    }
    if (path.endsWith('/clarification-draft') && revision) {
      expect(edit.request_id).toBe('revision-assessment')
      expect(edit.version).toBe(revision.clarification_draft?.version)
      revision.clarification_draft = { text: edit.text, version: edit.version + 1 }
      return Response.json(revision.clarification_draft)
    }
    if (path.endsWith('/retry') && revision && assessedOriginal.feedback?.status === 'assessed') {
      expect(edit.request_id).toBe('revision-assessment')
      if (outcome === 'needs_clarification') {
        expect({ text: edit.text, version: edit.version }).toEqual(revision.clarification_draft)
        revision.assessed_text = edit.text
        revision.clarification_draft = null
        revision.submission_version_id = 'clarified-revision'
      }
      revision.request = {
        ...revision.request,
        id: 'completed-revision-assessment', state: 'assessed', grade: 3,
        feedback: { ...assessedOriginal.feedback, summary: 'The use of a final test set is clear.' },
      }
      return result()
    }
    throw new Error(`Unexpected request ${path}`)
  })
  today.submitted.push({
    ...item('previous', 'Generalisation', '', 0, '2026-11-11'),
    submitted: true, grading_status: 'assessed',
  })
  today.first_use_date = '2026-11-11'
  today.completion_counts = { '2026-11-11': 1 }
  return { original, fetch }
}

describe('Assisted revision', () => {
  it('reopens an older submission, saves the revision on navigation, and compares both feedback results after retry', async () => {
    const { original, fetch } = revisionApi()
    const { unmount } = render(<App />)
    fireEvent.click(await screen.findByRole('button', { name: /Generalisation.*Assessed/ }))
    fireEvent.click(await screen.findByRole('button', { name: 'Revise answer' }))
    const editor = screen.getByRole('textbox', { name: 'Revised answer' })
    expect(editor).toHaveProperty('value', original)
    const revisedText = 'After choosing the model, use a separate final test set.'
    fireEvent.change(editor, { target: { value: revisedText } })

    fireEvent.click(screen.getByRole('button', { name: /Data leakage/ }))
    await screen.findByRole('heading', { name: 'Data leakage' })
    fireEvent.click(screen.getByRole('button', { name: /Generalisation.*Assessed/ }))
    const reopen = await screen.findByRole('button', { name: 'Revise answer' })
    fireEvent.click(reopen)
    expect(screen.getByRole('textbox', { name: 'Revised answer' })).toHaveProperty('value', revisedText)

    fireEvent.click(screen.getByRole('button', { name: 'Send revised answer' }))
    const originalAttempt = await screen.findByRole('region', { name: 'Original attempt' })
    const assistedRevision = await screen.findByRole('region', { name: 'Assisted revision' })
    await waitFor(() => expect(document.activeElement).toBe(within(assistedRevision).getByRole('heading', {
      name: 'Assisted revision',
    })))
    expect(within(originalAttempt).getByText(original)).toBeTruthy()
    expect(within(originalAttempt).getByText('Grade 2 of 3')).toBeTruthy()
    expect(within(assistedRevision).getByText(revisedText)).toBeTruthy()
    expect(within(assistedRevision).getByText('Grading outcome unknown')).toBeTruthy()
    fireEvent.click(within(assistedRevision).getByRole('button', { name: 'Retry grading' }))
    expect(await within(assistedRevision).findByText('Grade 3 of 3')).toBeTruthy()
    expect(within(originalAttempt).getByText('Explain when the test set is used.')).toBeTruthy()
    expect(within(assistedRevision).getByText('The use of a final test set is clear.')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Revise answer' })).toBeNull()
    expect(screen.getByText('No practice yet today')).toBeTruthy()
    expect(fetch.mock.calls.filter(([path]) => path.endsWith('/revise'))).toHaveLength(1)

    unmount()
    render(<App />)
    fireEvent.click(await screen.findByRole('button', { name: /Generalisation.*Assessed/ }))
    const savedRevision = await screen.findByRole('region', { name: 'Assisted revision' })
    expect(within(savedRevision).getByText(revisedText)).toBeTruthy()
    expect(within(savedRevision).getByText('Grade 3 of 3')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Revise answer' })).toBeNull()
  })

  it('keeps the local revision copyable when another tab takes the only revision', async () => {
    revisionApi('conflict')
    render(<App />)
    fireEvent.click(await screen.findByRole('button', { name: /Generalisation.*Assessed/ }))
    fireEvent.click(await screen.findByRole('button', { name: 'Revise answer' }))
    const editor = screen.getByRole('textbox', { name: 'Revised answer' })
    const text = 'My version explains the split more directly.'
    fireEvent.change(editor, { target: { value: text } })
    fireEvent.click(screen.getByRole('button', { name: 'Send revised answer' }))

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('This revision is closed.')
    expect(editor).toHaveProperty('value', text)
    expect(editor).toHaveProperty('readOnly', true)
    await waitFor(() => expect(document.activeElement).toBe(alert))
    fireEvent.click(screen.getByRole('button', { name: 'Copy my answer' }))
    await screen.findByText(/Your answer is selected/)
    expect(editor).toHaveProperty('selectionEnd', text.length)
    fireEvent.click(screen.getByRole('button', { name: 'Discard edit and refresh grading status' }))
    expect(await screen.findByText('The answer submitted in the other tab.')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Revise answer' })).toBeNull()
  })

  it('clarifies the revised answer inside the assisted revision and retains its first submitted text', async () => {
    const { fetch } = revisionApi('needs_clarification')
    render(<App />)
    fireEvent.click(await screen.findByRole('button', { name: /Generalisation.*Assessed/ }))
    fireEvent.click(await screen.findByRole('button', { name: 'Revise answer' }))
    const revised = 'Use a final test set.'
    fireEvent.change(screen.getByRole('textbox', { name: 'Revised answer' }), { target: { value: revised } })
    fireEvent.click(screen.getByRole('button', { name: 'Send revised answer' }))

    const revision = await screen.findByRole('region', { name: 'Assisted revision' })
    expect(within(revision).getByText('What was held out?')).toBeTruthy()
    const clarification = 'Reserve the test set until all model choices are final.'
    const editor = within(revision).getByRole('textbox', { name: 'Clarified answer' })
    expect(editor).toHaveProperty('value', revised)
    fireEvent.change(editor, { target: { value: clarification } })
    fireEvent.click(within(revision).getByRole('button', { name: 'Send clarified answer' }))

    expect(await within(revision).findByText('Grade 3 of 3')).toBeTruthy()
    expect(within(revision).getByText('First revised answer')).toBeTruthy()
    expect(within(revision).getByText(revised)).toBeTruthy()
    expect(within(revision).getByText('Clarified revision')).toBeTruthy()
    expect(within(revision).getByText(clarification)).toBeTruthy()
    const original = screen.getByRole('region', { name: 'Original attempt' })
    expect(within(original).getByText('Grade 2 of 3')).toBeTruthy()
    expect(fetch.mock.calls.filter(([path]) => path.endsWith('/revise'))).toHaveLength(1)
  })

  it('preserves both drafts when a status read sees a newer save before the local acknowledgement arrives', async () => {
    const { fetch } = revisionApi()
    const respond = fetch.getMockImplementation()!
    let reads = 0
    let resolveStatus: (response: Response) => void
    let resolveSave: (response: Response) => void
    fetch.mockImplementation(async (path, options) => {
      if (path.endsWith('/previous/submission') && ++reads === 2) {
        return new Promise(resolve => { resolveStatus = resolve })
      }
      if (path.endsWith('/revision-draft')) return new Promise(resolve => { resolveSave = resolve })
      return respond(path, options)
    })
    render(<App />)
    fireEvent.click(await screen.findByRole('button', { name: /Generalisation.*Assessed/ }))
    await screen.findByRole('button', { name: 'Revise answer' })
    fireEvent.click(screen.getByRole('link', { name: 'Settings' }))
    await screen.findByRole('heading', { name: 'Settings' })
    fireEvent.click(screen.getByRole('link', { name: 'Today' }))
    await waitFor(() => expect(reads).toBe(2))
    fireEvent.click(screen.getByRole('button', { name: 'Revise answer' }))
    const editor = screen.getByRole('textbox', { name: 'Revised answer' })
    const localText = 'The text I saved in this tab.'
    const newText = 'Newer text saved in the other tab.'
    fireEvent.change(editor, { target: { value: localText } })
    fireEvent.click(screen.getByRole('button', { name: 'Save draft' }))
    await waitFor(() => expect(resolveSave).toBeDefined())
    const earlier = await respond('/api/practice/items/previous/submission')
    const saved: SavedSubmission = await earlier.json()
    await act(async () => resolveStatus(Response.json({ ...saved, revision_draft: { text: newText, version: 2 } })))
    await act(async () => resolveSave(Response.json({ text: localText, version: 1 })))

    expect(await screen.findByRole('textbox', { name: 'Saved in another tab' })).toHaveProperty('value', newText)
    expect(editor).toHaveProperty('value', localText)
    expect(screen.getByRole('button', { name: 'Send revised answer' })).toHaveProperty('disabled', true)
  })
})
