import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { LocalRequest } from './api'
import { QuestionBrowser } from './QuestionBrowser'

const sections = [
  { id: 'fundamentals', title: 'Fundamentals', question_count: 2 },
  { id: 'forests', title: 'Forests', question_count: 1 },
  { id: 'deployment', title: 'Deployment', question_count: 0 },
]
const prompts = {
  fundamentals: [
    { id: 'generalisation', version: 1, title: 'Generalisation', difficulty: 'introductory',
      prompt: 'How should we evaluate new rentals?', objective: 'Explain generalisation.' },
    { id: 'data-leakage', version: 1, title: 'Data leakage', difficulty: 'introductory',
      prompt: 'What does the test split know?', objective: 'Identify leakage.' },
  ],
  forests: [{ id: 'tree-diversity', version: 1, title: 'Tree diversity', difficulty: 'introductory',
    prompt: 'What happens when errors are correlated?', objective: 'Explain averaging.' }],
  deployment: [],
}

function mockRequest() {
  const spy = vi.fn(async (path: string, _options?: RequestInit) => {
    if (path === '/api/sections') return sections
    const section = new URL(path, 'http://127.0.0.1').searchParams.get('section_id')
    if (section && section in prompts) return prompts[section as keyof typeof prompts]
    throw new Error('Unexpected route')
  })
  const request: LocalRequest = async <T,>(path: string, options?: RequestInit) =>
    await spy(path, options) as T
  return { request, spy }
}

afterEach(cleanup)

describe('Question browser', () => {
  it('shows one prompt, its objective and difficulty and lets the learner switch questions and sections', async () => {
    const { request, spy } = mockRequest()
    render(<QuestionBrowser request={request} />)
    expect(await screen.findByRole('heading', { name: 'Generalisation' })).toBeTruthy()
    expect(screen.getByText('How should we evaluate new rentals?')).toBeTruthy()
    expect(screen.getByText('Explain generalisation.')).toBeTruthy()
    expect(screen.getByText('Introductory')).toBeTruthy()

    fireEvent.change(screen.getByRole('combobox', { name: 'Question' }), { target: { value: 'data-leakage' } })
    expect(screen.getByRole('heading', { name: 'Data leakage' })).toBeTruthy()
    expect(screen.queryByText('How should we evaluate new rentals?')).toBeNull()
    expect(spy.mock.calls.filter(([path]) => path.startsWith('/api/questions'))).toHaveLength(1)

    fireEvent.change(screen.getByRole('combobox', { name: 'Section' }), { target: { value: 'forests' } })
    expect(await screen.findByText('Explain averaging.')).toBeTruthy()
    expect(screen.queryByText('Identify leakage.')).toBeNull()
    expect(screen.queryByRole('button', { name: /hint|explanation/i })).toBeNull()
  })

  it('gives an empty section a message and leaves section selection available', async () => {
    render(<QuestionBrowser request={mockRequest().request} />)
    await screen.findByRole('heading', { name: 'Generalisation' })
    const section = screen.getByRole('combobox', { name: 'Section' })
    fireEvent.change(section, { target: { value: 'deployment' } })
    expect(await screen.findByText('This section has no available questions. Choose another section.')).toBeTruthy()
    expect(screen.queryByRole('combobox', { name: 'Question' })).toBeNull()
    fireEvent.change(section, { target: { value: 'forests' } })
    expect(await screen.findByRole('heading', { name: 'Tree diversity' })).toBeTruthy()
  })

  it('reports an empty curriculum without requesting questions', async () => {
    const { request, spy } = mockRequest()
    spy.mockResolvedValueOnce([])
    render(<QuestionBrowser request={request} />)
    expect(await screen.findByText('No question sections are available.')).toBeTruthy()
    expect(spy).toHaveBeenCalledTimes(1)
  })

  it('recovers separately from section and prompt failures without exposing raw error text', async () => {
    const { request, spy } = mockRequest()
    spy.mockRejectedValueOnce(new Error('private SQLite error'))
    render(<QuestionBrowser request={request} />)
    expect((await screen.findByRole('alert')).textContent).toBe('Could not load sections.')
    expect(screen.queryByText('private SQLite error')).toBeNull()

    spy.mockResolvedValueOnce(sections).mockRejectedValueOnce(new Error('private question error'))
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect((await screen.findByRole('alert')).textContent).toBe('Could not load questions.')
    expect(screen.queryByText('private question error')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByRole('heading', { name: 'Generalisation' })).toBeTruthy()
  })

  it('does not restore a stale response when a learner switches sections quickly', async () => {
    const { request, spy } = mockRequest()
    let finish!: (value: typeof prompts.fundamentals) => void
    spy.mockResolvedValueOnce(sections).mockImplementationOnce(
      () => new Promise((resolve) => { finish = resolve }),
    )
    render(<QuestionBrowser request={request} />)
    const section = await screen.findByRole('combobox', { name: 'Section' })
    fireEvent.change(section, { target: { value: 'forests' } })
    await screen.findByRole('heading', { name: 'Tree diversity' })
    await act(async () => finish(prompts.fundamentals))
    expect(screen.queryByRole('heading', { name: 'Generalisation' })).toBeNull()
  })
})
