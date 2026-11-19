import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, test, vi } from 'vitest'
import type { LocalRequest } from './api'
import { PracticeFeedback, type SavedSubmission } from './PracticeFeedback'

afterEach(cleanup)

function assessment(id: string, summary: string): SavedSubmission['request'] {
  return {
    id, state: 'assessed', grade: 2, can_send: false, message: null,
    reference: { sources: {} },
    feedback: {
      status: 'assessed', summary, corrections: [], model_answer: 'Use held-out data.',
      reasoning: [], example: '', source_ids: [],
    },
  }
}

function initialSubmission(): SavedSubmission {
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
