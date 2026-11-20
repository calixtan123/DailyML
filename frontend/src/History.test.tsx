import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { LocalRequest } from './api'
import { History } from './History'

const entries = [
  { item_id: 'saved-1', local_date: '2026-11-17', section_id: 'fundamentals', section_title: 'Fundamentals', title: 'Generalisation', status: 'assessed', completed_date: '2026-11-17' },
]

function detail(itemId = 'saved-1') {
  return {
    item_id: itemId,
    local_date: '2026-11-17',
    question: {
      id: 'generalisation', version: 2, title: 'Generalisation', prompt: 'How should we test a model?',
      objective: 'Explain generalisation.', section_title: 'Fundamentals', concept_id: 'generalisation',
      criteria: [{ id: 'r1', description: 'Mentions held-out data', weight: 1 }],
      sources: [{ id: 's1', title: 'Source note', url: 'https://example.test/source' }], reference_explanation: 'Use a held-out set.',
    },
    draft: { text: 'Saved draft', version: 3 },
    submission: {
      assessed_text: 'Original saved answer', text: 'Original saved answer', completed_date: '2026-11-17', completed_timezone: 'Europe/London', credited: true,
      submission_version_id: 'v1', supported: false, clarification_draft: null, revision_draft: null,
      request: { id: 'request-1', state: 'assessed', grade: 2, can_send: false, message: null, reference: { sources: {} }, feedback: { status: 'assessed', summary: 'Good reasoning', corrections: ['Name the held-out split'], model_answer: 'Use new data.', reasoning: ['Split first'], example: 'A validation set', source_ids: [] } },
      versions: [
        { id: 'v1', role: 'original', kind: 'initial', parent_version_id: null, text: 'Original saved answer', supported: false, created_at: 1, request: { id: 'request-1', state: 'assessed', grade: 2, can_send: false, message: null, finished_at: 1_794_998_400, reference: { sources: {} }, feedback: { status: 'assessed', summary: 'Original version explanation', corrections: [], model_answer: 'Version model answer.', reasoning: ['Version reasoning'], example: 'Version example', source_ids: [] } } },
        { id: 'v2', role: 'original', kind: 'clarification', parent_version_id: 'v1', text: 'Clarified saved answer', supported: false, created_at: 2, request: { id: 'request-2', state: 'assessed', grade: 3, can_send: false, message: null, finished_at: 1_794_998_401, reference: { sources: {} }, feedback: null } },
      ],
      revision: null,
      disputes: [{ id: 'dispute-1', status: 'resolved', target_request_id: 'request-1', role: 'original', selected_request_id: 'assessment-2', objection: 'The rubric missed my split.', assessments: [
        { id: 'request-1', state: 'assessed', grade: 2, can_send: false, message: null, reference: { sources: {} }, feedback: { status: 'assessed', summary: 'Original comparison explanation', corrections: [], model_answer: 'Original model answer.', reasoning: ['Original reasoning'], example: 'Original example', source_ids: [] } },
        { id: 'assessment-2', state: 'assessed', grade: 3, can_send: false, message: null, reference: { sources: {} }, feedback: { status: 'assessed', summary: 'Reassessment comparison explanation', corrections: ['Explains the comparison'], model_answer: 'Reassessed model answer.', reasoning: ['Reassessed reasoning'], example: 'Reassessed example', source_ids: [] } },
      ] }],
    },
    support: { reveals: ['hint'], gap_completed: false }, assessed_at: 1_794_998_400,
    review: { due_date: '2026-12-01', awaiting_assessment: false },
  }
}

function mockRequest(handler?: (path: string) => unknown) {
  const spy = vi.fn(async (path: string, _options?: RequestInit) => {
    if (handler) return handler(path)
    if (path === '/api/history/sections') return [{ id: 'fundamentals', title: 'Fundamentals' }]
    if (path.startsWith('/api/history/saved-1')) return detail()
    if (path.startsWith('/api/history')) return { entries, next_cursor: null }
    throw new Error(`Unexpected path ${path}`)
  })
  const request: LocalRequest = async <T,>(path: string, options?: RequestInit) => await spy(path, options) as T
  return { request, spy }
}

afterEach(cleanup)

describe('History', () => {
  it('loads saved history with labelled filters and submits the selected query', async () => {
    const { request, spy } = mockRequest()
    render(<History request={request} onResume={vi.fn()} />)
    expect(await screen.findByRole('button', { name: 'Generalisation' })).toBeTruthy()
    fireEvent.change(screen.getByRole('combobox', { name: 'Section' }), { target: { value: 'fundamentals' } })
    fireEvent.change(screen.getByLabelText('From date'), { target: { value: '2026-11-01' } })
    fireEvent.change(screen.getByLabelText('To date'), { target: { value: '2026-11-18' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply filters' }))
    await screen.findByRole('button', { name: 'Generalisation' })
    expect(spy).toHaveBeenCalledWith('/api/history?section_id=fundamentals&start_date=2026-11-01&end_date=2026-11-18', expect.anything())
  })

  it('shows an empty result and a safe generic request error', async () => {
    const empty = mockRequest(path => path === '/api/history/sections' ? [] : { entries: [], next_cursor: null })
    render(<History request={empty.request} onResume={vi.fn()} />)
    expect(await screen.findByText('No saved learning matches these filters.')).toBeTruthy()
    cleanup()
    const failed = mockRequest(path => { if (path === '/api/history/sections') return []; throw new Error('private sqlite error') })
    render(<History request={failed.request} onResume={vi.fn()} />)
    expect((await screen.findByRole('alert')).textContent).toBe('Could not load saved history. Change a filter or try again.')
    expect(screen.queryByText('private sqlite error')).toBeNull()
  })

  it('retries the same filters after an index request fails', async () => {
    let attempts = 0
    const { request } = mockRequest(path => {
      if (path === '/api/history/sections') return []
      attempts += 1
      if (attempts === 1) throw new Error('temporary local failure')
      return { entries, next_cursor: null }
    })
    render(<History request={request} onResume={vi.fn()} />)
    expect((await screen.findByRole('alert')).textContent).toBe('Could not load saved history. Change a filter or try again.')
    fireEvent.click(screen.getByRole('button', { name: 'Apply filters' }))
    expect(await screen.findByRole('button', { name: 'Generalisation' })).toBeTruthy()
    expect(attempts).toBe(2)
  })

  it('keeps the first page visible while loading and retrying the next cursor page', async () => {
    let resolveNext!: (value: { entries: typeof entries; next_cursor: null }) => void
    let nextAttempts = 0
    const { request, spy } = mockRequest(path => {
      if (path === '/api/history/sections') return []
      if (path === '/api/history?cursor=next-page') {
        nextAttempts += 1
        if (nextAttempts === 1) return new Promise((_, reject) => { reject(new Error('temporary page failure')) })
        return new Promise<{ entries: typeof entries; next_cursor: null }>(resolve => { resolveNext = resolve })
      }
      return { entries, next_cursor: 'next-page' }
    })
    render(<History request={request} onResume={vi.fn()} />)
    expect(await screen.findByRole('button', { name: 'Generalisation' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Load more history' }))
    expect((await screen.findByRole('alert')).textContent).toContain('Could not load more saved history.')
    expect(screen.getByRole('button', { name: 'Generalisation' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Retry loading more' }))
    expect((await screen.findByRole('status')).textContent).toBe('Loading more saved history…')
    expect(screen.getByRole('button', { name: 'Generalisation' })).toBeTruthy()
    await act(async () => resolveNext({ entries: [{ ...entries[0], item_id: 'saved-2', title: 'Data leakage' }], next_cursor: null }))
    expect(await screen.findByRole('button', { name: 'Data leakage' })).toBeTruthy()
    expect(spy).toHaveBeenCalledWith('/api/history?cursor=next-page', expect.anything())
  })

  it('ignores a stale page when filters replace an in-flight request', async () => {
    let resolveFirst!: (value: { entries: typeof entries; next_cursor: null }) => void
    const { request } = mockRequest(path => {
      if (path === '/api/history/sections') return [{ id: 'fundamentals', title: 'Fundamentals' }]
      if (path === '/api/history') return new Promise(resolve => { resolveFirst = resolve })
      if (path === '/api/history?section_id=fundamentals') return { entries: [{ ...entries[0], title: 'Filtered record' }], next_cursor: null }
      throw new Error(`Unexpected path ${path}`)
    })
    render(<History request={request} onResume={vi.fn()} />)
    await screen.findByRole('combobox', { name: 'Section' })
    fireEvent.change(screen.getByRole('combobox', { name: 'Section' }), { target: { value: 'fundamentals' } })
    fireEvent.click(screen.getByRole('button', { name: 'Apply filters' }))
    expect(await screen.findByRole('button', { name: 'Filtered record' })).toBeTruthy()
    await act(async () => resolveFirst({ entries, next_cursor: null }))
    expect(screen.queryByRole('button', { name: 'Generalisation' })).toBeNull()
  })

  it('shows exact saved evidence and resumes the selected item', async () => {
    const onResume = vi.fn()
    const { request, spy } = mockRequest()
    render(<History request={request} onResume={onResume} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Generalisation' }))
    expect(await screen.findByRole('heading', { name: 'Generalisation' })).toBeTruthy()
    expect(spy).toHaveBeenCalledWith('/api/history/saved-1', expect.anything())
    expect(screen.getByText('How should we test a model?')).toBeTruthy()
    expect(screen.getAllByText('Original saved answer')).not.toHaveLength(0)
    expect(screen.getByText('Good reasoning')).toBeTruthy()
    expect(screen.getByText('Independent answer — no learning support was used before submission.')).toBeTruthy()
    expect(screen.getByText('Question version 2')).toBeTruthy()
    expect(screen.getByText('Current review date')).toBeTruthy()
    expect(screen.getByText('The rubric missed my split.')).toBeTruthy()
    expect(screen.getByText('Selected assessment: Reassessment.')).toBeTruthy()
    expect(screen.getByText('Reassessment comparison explanation')).toBeTruthy()
    fireEvent.click(screen.getByText('Answer versions', { exact: true }))
    expect(screen.getByText('Original version explanation')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Resume practice' }))
    expect(onResume).toHaveBeenCalledWith('saved-1')
  })

  it('labels a sendable saved response as recoverable', async () => {
    const { request } = mockRequest(path => {
      if (path === '/api/history/sections') return []
      if (path === '/api/history/saved-1') {
        const saved = detail()
        saved.submission.request.can_send = true
        return saved
      }
      return { entries, next_cursor: null }
    })
    render(<History request={request} onResume={vi.fn()} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Generalisation' }))
    expect(await screen.findByRole('button', { name: 'Recover saved answer' })).toBeTruthy()
  })

  it('keeps an unfinished item’s saved marking guide behind resumable practice', async () => {
    const { request } = mockRequest(path => {
      if (path === '/api/history/sections') return []
      if (path === '/api/history/saved-1') {
        return { ...detail(), submission: null, support: null }
      }
      return { entries, next_cursor: null }
    })
    render(<History request={request} onResume={vi.fn()} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Generalisation' }))
    expect(await screen.findByText('Resume practice to open the saved marking guide and learning support.')).toBeTruthy()
    expect(screen.queryByText('Use a held-out set.')).toBeNull()
    expect(screen.queryByText('Question version 2')).toBeNull()
  })

  it('ignores a stale detail response after a newer history item is opened', async () => {
    let resolveFirst!: (value: ReturnType<typeof detail>) => void
    const { request } = mockRequest(path => {
      if (path === '/api/history/sections') return []
      if (path === '/api/history') return { entries: [{ ...entries[0], item_id: 'saved-1' }, { ...entries[0], item_id: 'saved-2', title: 'Data leakage' }], next_cursor: null }
      if (path === '/api/history/saved-1') return new Promise<ReturnType<typeof detail>>(resolve => { resolveFirst = resolve })
      if (path === '/api/history/saved-2') {
        const saved = detail('saved-2')
        saved.question.title = 'Data leakage'
        return saved
      }
      throw new Error(`Unexpected path ${path}`)
    })
    render(<History request={request} onResume={vi.fn()} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Generalisation' }))
    fireEvent.click(screen.getByRole('button', { name: 'Data leakage' }))
    expect(await screen.findByRole('heading', { name: 'Data leakage' })).toBeTruthy()
    await act(async () => resolveFirst(detail('saved-1')))
    expect(screen.getByRole('heading', { name: 'Data leakage' })).toBeTruthy()
  })
})
