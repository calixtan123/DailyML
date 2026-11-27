import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import type { LocalRequest } from './api'
import { PracticeFeedback, type SavedSubmission } from './PracticeFeedback'

afterEach(cleanup)

type Assessment = Extract<SavedSubmission['request'], { id: string }>

function assessment(id: string, summary: string): Assessment {
  return {
    id, state: 'assessed', grade: 2, can_send: false, message: null,
    reference: { sources: {} },
    feedback: {
      status: 'assessed', summary, corrections: [], model_answer: 'Use held-out data.',
      reasoning: [], example: '', source_ids: [],
    },
  }
}

function initialSubmission(): SavedSubmission & { request: Assessment } {
  return {
    text: 'Keep a test set.', assessed_text: 'Keep a test set.', completed_date: '2026-11-19',
    completed_timezone: 'Europe/London', credited: true, submission_version_id: 'version',
    supported: false, clarification_draft: null, versions: [], revision: null, revision_draft: null,
    request: assessment('first', 'Original explanation.'),
  }
}

test('opens a dispute, compares a reassessment and accepts it explicitly', async () => {
  const submission = initialSubmission()
  const objection = 'The answer describes evaluation on held-out data.'
  const dispute = {
    id: 'dispute', status: 'open', target_request_id: 'first', target_version_id: 'version',
    role: 'original', objection, selected_request_id: null as string | null,
    assessments: [submission.request],
  }
  const request: LocalRequest = async <T,>(path: string, options?: RequestInit) => {
    if (path.endsWith('/submission')) return structuredClone(submission) as T
    if (path.endsWith('/disputes')) {
      expect(JSON.parse(String(options?.body))).toEqual({ request_id: 'first', objection })
    } else if (path.endsWith('/reassess')) {
      dispute.assessments.push(assessment('second', 'Reassessed explanation.'))
    } else if (path.endsWith('/resolve')) {
      expect(JSON.parse(String(options?.body))).toEqual({ assessment_request_id: 'second' })
      dispute.status = 'resolved'
      dispute.selected_request_id = 'second'
    }
    Object.assign(submission, { disputes: [structuredClone(dispute)] })
    return structuredClone(submission) as T
  }
  render(<PracticeFeedback itemId="item" active request={request} initial={submission}
    answerLimit={8000} navigationPending={false} leaveEditor={{ current: null }} onStatus={vi.fn()} />)

  const original = within(screen.getByRole('region', { name: 'Original attempt', exact: true }))
  expect(original.getByRole('heading', { name: 'What to improve' })).toBeTruthy()
  expect(original.getByText('No corrections recorded.')).toBeTruthy()

  fireEvent.click(screen.getByRole('button', { name: 'Dispute assessment' }))
  fireEvent.change(screen.getByLabelText('Your objection'), { target: { value: objection } })
  fireEvent.click(screen.getByRole('button', { name: 'Open dispute' }))
  fireEvent.click(await screen.findByRole('button', { name: 'Request reassessment' }))
  expect(await screen.findByText('Reassessed explanation.')).toBeTruthy()
  expect(screen.getByText('Original explanation.')).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Accept reassessment' }))
  expect(await screen.findByText('Reassessment accepted.')).toBeTruthy()
  expect(screen.getByText(objection)).toBeTruthy()
})

test('shows the latest reassessment and reuses its action key after a lost response', async () => {
  const submission = initialSubmission()
  const firstFailure = {
    ...assessment('old-retry', ''), state: 'failed_recoverable' as const,
    feedback: null, message: 'Earlier retry failed.', can_send: true,
  }
  const currentFailure = { ...firstFailure, id: 'latest-retry', message: 'Latest retry failed.' }
  const dispute = {
    id: 'dispute', status: 'open' as const, target_request_id: 'first',
    role: 'original' as const, objection: 'Check the rubric.', selected_request_id: null,
    assessments: [submission.request, firstFailure, currentFailure],
  }
  submission.disputes = [dispute]
  const keys: string[] = []
  const request: LocalRequest = async <T,>(path: string, options?: RequestInit) => {
    if (path.endsWith('/reassess')) {
      keys.push(JSON.parse(String(options?.body)).idempotency_key)
      if (keys.length === 1) throw new Error('Response lost')
      dispute.assessments.push(assessment('reassessment', 'Updated explanation.'))
    }
    return structuredClone(submission) as T
  }
  render(<PracticeFeedback itemId="item" active request={request} initial={submission}
    answerLimit={8000} navigationPending={false} leaveEditor={{ current: null }} onStatus={vi.fn()} />)

  expect(await screen.findByText('Latest retry failed.')).toBeTruthy()
  expect(screen.queryByText('Earlier retry failed.')).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: 'Retry reassessment' }))
  expect(await screen.findByText(/Could not confirm the dispute action/)).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Retry reassessment' }))
  expect(await screen.findByText('Updated explanation.')).toBeTruthy()
  expect(keys[0]).toEqual(expect.any(String))
  expect(keys).toEqual([keys[0], keys[0]])
})
