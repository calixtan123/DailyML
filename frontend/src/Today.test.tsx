import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
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

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  window.history.replaceState(null, '', '/')
})

describe('Today practice editor', () => {
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
        text: 'Stored original', assessed_text: 'Stored original', credited: true,
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
    expect(screen.getByText('State the split.')).toBeTruthy()
    expect(screen.getByRole('link', { name: 'reading', hidden: true })).toHaveProperty('href', 'https://example.org/')
    expect(fetch.mock.calls.filter(([path]) => path.endsWith('/retry') || path.endsWith('/submit'))).toHaveLength(0)
  })
})

type TestDraft = { text: string; version: number }
const originalClarifiedAnswer = 'Keep separate data.'
const clarificationRequest: SavedSubmission['request'] = { id: 'clarification', state: 'needs_clarification',
  feedback: { status: 'needs_clarification', explanation: 'What was held out?' },
  message: null, grade: null, can_send: false, reference: { sources: {} },
}

function clarificationApi(
  save?: (edit: TestDraft) => Response,
  read?: (result: Response) => Response | Promise<Response>,
) {
  let draft: TestDraft | null = { text: originalClarifiedAnswer, version: 0 }
  let request: SavedSubmission['request'] = clarificationRequest
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
    fireEvent.click(screen.getByRole('button', { name: 'Save draft' }))
    await screen.findByText('Draft saved.')
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
    fireEvent.click(screen.getByRole('button', { name: /Generalisation/ }))

    expect(await screen.findByRole('textbox', { name: 'Clarified answer' })).toHaveProperty('value', text)
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
    expect(document.activeElement).toBe(alert)
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
    expect(screen.getByRole('button', { name: /Model monitoring.*Needs retry/ })).toBeTruthy()
    const select = screen.getByRole('combobox', { name: 'Session question' })
    expect(within(select).getByRole('option', { name: /Generalisation.*Needs clarification/ })).toBeTruthy()
    expect(within(select).getByRole('option', { name: /Model monitoring.*Needs retry/ })).toBeTruthy()
    expect(fetch.mock.calls.some(([path]) => path === '/api/practice/items/previous/submission')).toBe(false)
  })
})
