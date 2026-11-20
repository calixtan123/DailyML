import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { LocalApiError, type LocalRequest } from './api'
import { GradingSettings, type GradingAction } from './GradingSettings'

afterEach(() => { cleanup(); vi.useRealTimers() })

const question = { question: 'Which split?', answer: 'Training only.', sources: {} }
const saved = { question: 'Original question?', answer: 'Original answer.', sources: { guide: 'https://example.test/v1' } }
const models = { models: [{slug: 'model-z', display_name: 'Account model'}], selected: 'model-z', version: 0,
  overridden: false, source: 'preferences' as const }
const queued = { id: 'r1', state: 'queued', model_id: 'model-z', feedback: null, grade: null, reference: saved }
const assessed = { ...queued, state: 'assessed', grade: 3, feedback: { status: 'assessed',
  summary: 'Training data only.', corrections: [], model_answer: 'Fit on training.', reasoning: ['Hold out test data.'],
  example: 'Use the training mean.', criteria: [], source_ids: [], follow_up: [], critical_misconception_ids: [] } }

function GradingPanel({ request, initialAction }: { request: LocalRequest; initialAction?: GradingAction }) {
  const [uncertainAction, setUncertainAction] = useState(initialAction)
  return <GradingSettings request={request} connected uncertainAction={uncertainAction}
    onUncertainActionChange={setUncertainAction} />
}

function setup(latest: object | null = null, { catalog = models, initialAction }: {
  catalog?: typeof models | Error
  initialAction?: GradingAction
} = {}) {
  let runs = 0
  let reads = 0
  const sent: Record<string, string>[] = []
  const api = vi.fn(async (path: string, options?: RequestInit) => {
    if (path.endsWith('/models')) {
      if (catalog instanceof Error) throw catalog
      return catalog
    }
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
  const view = render(<GradingPanel request={api as LocalRequest} initialAction={initialAction} />)
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

  it('keeps the saved uncertain outcome visible when observed usage cannot be loaded', async () => {
    const unknown = { ...queued, state: 'outcome_unknown', error: 'outcome_unknown',
      message: 'The stream was interrupted. The provider may have used a plan request.' }
    let usageAvailable = false
    const request = vi.fn(async (path: string, options?: RequestInit) => {
      if (path.endsWith('/models')) return models
      if (path.endsWith('/usage')) {
        if (!usageAvailable) throw new Error('Could not load usage.')
        return { attempts_24h: 4 }
      }
      if (path.endsWith('/test') && options?.method === 'POST') throw new Error('Could not reach the local app.')
      if (path.endsWith('/test')) return { reference: question, latest: unknown }
      throw new Error('Unexpected route')
    })
    render(<GradingPanel request={request as LocalRequest} />)
    expect((await screen.findByRole('alert')).textContent).toBe(unknown.message)
    expect(screen.getByRole('button', { name: 'Retry connection test' })).toHaveProperty('disabled', false)
    expect(screen.getByText('Model attempts in the last 24 hours: unavailable')).toBeTruthy()

    const requestsBeforeRefresh = request.mock.calls.length
    usageAvailable = true
    fireEvent.click(screen.getByRole('button', { name: 'Refresh observed usage' }))
    expect(await screen.findByText('Model attempts in the last 24 hours: 4')).toBeTruthy()
    expect(screen.getByRole('alert').textContent).toBe(unknown.message)
    expect(request.mock.calls.slice(requestsBeforeRefresh).map(([path]) => path)).toEqual(['/api/usage'])

    fireEvent.click(screen.getByRole('button', { name: 'Retry connection test' }))
    await screen.findByText('Could not reach the local app.')
    expect(screen.getByText(unknown.message)).toBeTruthy()
    expect(document.activeElement).toBe(screen.getByRole('alert'))
  })

  it('recovers the retained test when the previously selected model is no longer available', async () => {
    vi.useFakeTimers()
    const initialAction = { model_id: 'model-z', idempotency_key: 'original-action', retry_of: 'r0' }
    const { sent } = setup(null, {
      catalog: { ...models, models: [{ slug: 'model-y', display_name: 'Available model' }] },
      initialAction,
    })

    await act(async () => {})
    expect(screen.getByRole('option', { name: 'Available model' })).toBeTruthy()
    expect(screen.getByRole('combobox', { name: 'Grading model' })).toHaveProperty('disabled', true)
    const recover = screen.getByRole('button', { name: 'Check previous test' })
    expect(recover).toHaveProperty('disabled', false)

    await act(async () => { fireEvent.click(recover) })
    expect(sent).toEqual([initialAction])
    await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
    expect(screen.getByRole('combobox', { name: 'Grading model' })).toHaveProperty('disabled', false)
    expect(screen.getByRole('button', { name: 'Run connection test' })).toHaveProperty('disabled', true)
  })

  it('recovers the retained test while the model catalog is unavailable', async () => {
    vi.useFakeTimers()
    const initialAction = { model_id: 'model-z', idempotency_key: 'original-action', retry_of: 'r0' }
    const { sent } = setup(null, { catalog: new Error('The model catalog is unavailable.'), initialAction })

    await act(async () => {})
    expect(screen.getByText('Training only.')).toBeTruthy()
    expect(screen.getByRole('alert').textContent).toBe('The model catalog is unavailable.')
    const recover = screen.getByRole('button', { name: 'Check previous test' })
    expect(recover).toHaveProperty('disabled', false)
    expect(sent).toEqual([])

    await act(async () => { fireEvent.click(recover) })
    await act(async () => { await vi.advanceTimersByTimeAsync(1000) })
    expect(screen.getByText(/Grade 3 of 3/)).toBeTruthy()
    expect(sent).toEqual([initialAction])
    expect(screen.queryByRole('button', { name: 'Run connection test' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Check previous test' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Refresh model catalog' })).toHaveProperty('disabled', false)
  })

  it('shows a removed model after refresh and only saves the replacement on confirmation', async () => {
    const selection = { selected: 'model-y', version: 1, source: 'preferences', overridden: false }
    let currentCatalog = models
    const writes: unknown[] = []
    const request = vi.fn(async (path: string, options?: RequestInit) => {
      if (path.endsWith('/models')) return currentCatalog
      if (path.endsWith('/usage')) return { attempts_24h: 4 }
      if (path.endsWith('/test')) return { reference: question, latest: null }
      if (path.endsWith('/model')) {
        writes.push(JSON.parse(String(options?.body)))
        return selection
      }
      throw new Error('Unexpected route')
    })
    render(<GradingPanel request={request as LocalRequest} />)
    const choice = await screen.findByRole('combobox', { name: 'Grading model' })
    expect(choice).toHaveProperty('value', 'model-z')
    const refresh = screen.getByRole('button', { name: 'Refresh model catalog' })
    currentCatalog = { ...models, models: [{ slug: 'model-y', display_name: 'Replacement model' }] }
    fireEvent.click(refresh)

    expect((await screen.findByRole('alert')).textContent).toMatch(/model-z.*unavailable/i)
    expect(screen.getByRole('button', { name: 'Run connection test' })).toHaveProperty('disabled', true)
    fireEvent.change(choice, { target: { value: 'model-y' } })
    expect(writes).toEqual([])
    fireEvent.click(screen.getByRole('button', { name: 'Save model' }))
    await screen.findByText('Model saved.')
    expect(writes).toEqual([{ model_id: 'model-y', version: 0 }])
    expect(screen.getByRole('button', { name: 'Run connection test' })).toHaveProperty('disabled', false)
  })

  it('focuses a failed model save and refreshes the catalog before another choice', async () => {
    const list = { ...models, models: [...models.models, { slug: 'other-model', display_name: 'Other model' }] }
    const request = vi.fn(async (path: string) => {
      if (path.endsWith('/models')) return list
      if (path.endsWith('/usage')) return { attempts_24h: 0 }
      if (path.endsWith('/test')) return { reference: question, latest: null }
      throw new LocalApiError(409, { detail: 'Preferences changed in another tab.' })
    })
    render(<GradingPanel request={request as LocalRequest} />)
    const choice = await screen.findByRole('combobox', { name: 'Grading model' })
    fireEvent.change(choice, { target: { value: 'other-model' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save model' }))
    const error = await screen.findByRole('alert')
    expect(document.activeElement).toBe(error)
    expect(screen.getByRole('button', { name: 'Save model' })).toHaveProperty('disabled', true)
    fireEvent.click(screen.getByRole('button', { name: 'Refresh model catalog' }))
    await screen.findByDisplayValue('Account model')
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('uses the saved sources in feedback while previewing the next fresh question', async () => {
    setup({ ...assessed, feedback: {...assessed.feedback, source_ids: ['guide']} })
    expect(await screen.findByText('Which split?')).toBeTruthy()
    expect(screen.getByRole('link', {name: 'guide', hidden: true}).getAttribute('href')).toBe('https://example.test/v1')
  })
})
