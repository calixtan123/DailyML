import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { LocalRequest } from './api'
import { GradingSettings } from './GradingSettings'

afterEach(() => { cleanup(); vi.useRealTimers() })

const question = { question: 'Which split?', answer: 'Training only.', sources: {} }
const saved = { question: 'Original question?', answer: 'Original answer.', sources: { guide: 'https://example.test/v1' } }
const models = { models: [{slug: 'model-z', display_name: 'Account model'}], selected: 'model-z', version: 0 }
const queued = { id: 'r1', state: 'queued', model_id: 'model-z', feedback: null, grade: null, reference: saved }
const assessed = { ...queued, state: 'assessed', grade: 3, feedback: { status: 'assessed',
  summary: 'Training data only.', corrections: [], model_answer: 'Fit on training.', reasoning: ['Hold out test data.'],
  example: 'Use the training mean.', criteria: [], source_ids: [], follow_up: [], critical_misconception_ids: [] } }

function setup(latest: object | null = null) {
  let runs = 0
  let reads = 0
  const sent: Record<string, string>[] = []
  const api = vi.fn(async (path: string, options?: RequestInit) => {
    if (path.endsWith('/models')) return models
    if (path.endsWith('/usage')) return {attempts_24h: runs}
    if (path.endsWith('/test') && options?.method === 'POST') {
      sent.push(JSON.parse(String(options.body)))
      runs++
      return queued
    }
    if (path.endsWith('/test')) return { reference: question, latest }
    if (path.endsWith('/requests/r1')) { reads++; return assessed }
    throw new Error('Unexpected path')
  })
  const view = render(<GradingSettings request={api as LocalRequest} connected />)
  return { sent, view, reads: () => reads }
}

describe('grading connection test', () => {
  it('shows the reference, account model and cost before sending, then renders the persisted grade', async () => {
    vi.useFakeTimers()
    const { sent, reads } = setup()
    await act(async () => {})
    expect(screen.getByLabelText('Grading model')).toBeTruthy()
    expect(screen.getByText('Training only.')).toBeTruthy()
    expect(screen.getByText(/sends.*OpenAI.*ChatGPT plan/i)).toBeTruthy()
    expect(sent).toEqual([])
    await act(async () => { fireEvent.click(screen.getByRole('button', {name: 'Run connection test'})) })
    await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
    expect(screen.getByText(/Grade 3 of 3/)).toBeTruthy()
    expect(screen.getByText('Training data only.')).toBeTruthy()
    expect(screen.getByText(/Model attempts in the last 24 hours: 1/)).toBeTruthy()
    await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
    expect(reads()).toBe(1)
  })

  it('offers an explicit schema recovery and stops polling on disposal', async () => {
    vi.useFakeTimers()
    const { sent, view, reads } = setup({ ...queued, state: 'failed_recoverable',
      error: 'schema_unsupported', message: 'This model rejected structured output.' })
    await act(async () => {})
    const retry = screen.getByRole('button', { name: 'Retry in JSON mode' })
    expect(screen.getByText('Original question?')).toBeTruthy()
    expect(sent).toHaveLength(0)
    await act(async () => { fireEvent.click(retry) })
    expect(sent[0].retry_of).toBe('r1')
    view.unmount()
    await act(async () => { await vi.advanceTimersByTimeAsync(5000) })
    expect(reads()).toBe(0)
  })

  it('uses the saved sources in feedback while previewing the next fresh question', async () => {
    setup({ ...assessed, feedback: {...assessed.feedback, source_ids: ['guide']} })
    expect(await screen.findByText('Which split?')).toBeTruthy()
    expect(screen.getByRole('link', {name: 'guide', hidden: true}).getAttribute('href')).toBe('https://example.test/v1')
  })
})
