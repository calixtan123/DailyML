import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { App } from './App'

const item = (id: string, title: string, text = '', version = 0, date = '2026-11-19') => ({
  id,
  local_date: date,
  question: {
    id, version: 1, title, prompt: `Explain ${title}.`, objective: 'Explain the main idea.',
    section_id: 'fundamentals', section_title: 'Fundamentals', concept_id: id,
    expected_answer_words: [30, 80], difficulty: 'introductory',
  },
  draft: { text, version },
})

function practiceApi(save?: (edit: { text: string; version: number }) => Promise<Response>) {
  const first = item('first', 'Generalisation')
  const second = item('second', 'Data leakage')
  const third = item('third', 'Model monitoring')
  const today = {
    local_date: '2026-11-19', timezone: 'Europe/Paris', effective_timezone: 'Europe/London',
    goal: 2, answer_limit: 8000, shortage: null,
    selections: [first, second].map(item => ({ item, kind: 'variety' })),
    unfinished: [],
  }
  const fetch = vi.fn(async (path: string, options?: RequestInit) => {
    if (path === '/api/session') return Response.json({ csrf_token: 'test' })
    if (path === '/api/practice/today') return Response.json(today)
    if (path === '/api/sections') {
      return Response.json([{ id: 'fundamentals', title: 'Fundamentals', question_count: 2 }])
    }
    if (path === '/api/practice/items') {
      today.selections.push({ item: third, kind: 'extra' })
      return Response.json({ today, item_id: 'third', message: null })
    }
    if (path === '/api/practice/items/first/draft' && options?.method === 'PUT') {
      const edit = JSON.parse(String(options.body))
      if (save) return save(edit)
      first.draft = { text: edit.text, version: edit.version + 1 }
      return Response.json(first.draft)
    }
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
})
