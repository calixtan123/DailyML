import { useEffect, useState } from 'react'
import type { LocalRequest } from './api'

type Section = { id: string; title: string; question_count: number }
type Question = {
  id: string
  version: number
  title: string
  prompt: string
  objective: string
  difficulty: 'introductory' | 'intermediate' | 'advanced'
  concept_id: string
}

type LoadState<T> =
  | { status: 'loading' }
  | { status: 'error' }
  | { status: 'ready'; value: T }

function useBankResource<T>(request: LocalRequest, path: string) {
  const [result, setResult] = useState<LoadState<T>>({ status: 'loading' })
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    request<T>(path, { signal: controller.signal }).then(
      (value) => {
        if (!controller.signal.aborted) setResult({ status: 'ready', value })
      },
      () => {
        if (!controller.signal.aborted) setResult({ status: 'error' })
      },
    )
    return () => controller.abort()
  }, [request, path, attempt])

  function retry() {
    setResult({ status: 'loading' })
    setAttempt((previous) => previous + 1)
  }

  return { result, retry }
}

function LoadNotice({ status, resource, retry }: {
  status: 'loading' | 'error'
  resource: 'sections' | 'questions'
  retry: () => void
}) {
  return (
    <div className="panel browser-state">
      {status === 'loading' ? <p role="status">Loading {resource}…</p> : (
        <>
          <p role="alert">Could not load {resource}.</p>
          <button type="button" onClick={retry}>Retry</button>
        </>
      )}
    </div>
  )
}

const difficultyLabels: Record<Question['difficulty'], string> = {
  introductory: 'Introductory',
  intermediate: 'Intermediate',
  advanced: 'Advanced',
}

type PracticeChoice = { section_id: string; question_id?: string }
export type ReviewDates = { today: string; byConcept: Record<string, { due_date: string | null }> }
type PracticeActions = {
  onPractice?: (choice: PracticeChoice) => void
  practiceDisabled?: boolean
  reviewDates?: ReviewDates
}

function SectionQuestions({ section, request, onPractice, practiceDisabled, reviewDates }: {
  section: Section
  request: LocalRequest
} & PracticeActions) {
  const path = '/api/questions?' + new URLSearchParams({ section_id: section.id })
  const { result, retry } = useBankResource<Question[]>(request, path)
  const [selectedQuestion, setSelectedQuestion] = useState('')

  if (result.status !== 'ready') return <LoadNotice status={result.status} resource="questions" retry={retry} />

  const questions = result.value
  const current = questions.find((question) => question.id === selectedQuestion) ?? questions[0]
  if (!current) {
    return <p className="panel browser-state" role="status">This section has no available questions. Choose another section.</p>
  }
  const dueDate = reviewDates?.byConcept[current.concept_id]?.due_date

  return (
    <div className="browser-grid">
      <div className="panel browser-question-picker">
        <label htmlFor="bank-question">Question</label>
        <select id="bank-question" value={current.id} onChange={(event) => setSelectedQuestion(event.target.value)}>
          {questions.map((question) => <option value={question.id} key={question.id}>{question.title}</option>)}
        </select>
      </div>
      <article className="panel browser-question" aria-labelledby="bank-question-title">
        <div className="browser-question-prompt">
          <h2 id="bank-question-title">{current.title}</h2>
          <p className="browser-difficulty">{difficultyLabels[current.difficulty]}</p>
          <p>{current.prompt}</p>
        </div>
        <div className="browser-objective">
          <h3>Learning objective</h3>
          <p>{current.objective}</p>
          {reviewDates && dueDate && (
            <p>{dueDate <= reviewDates.today ? 'Review due' : 'Next review'} <time dateTime={dueDate}>{dueDate}</time></p>
          )}
          {onPractice && (
            <button type="button" disabled={practiceDisabled}
              onClick={() => onPractice({ section_id: section.id, question_id: current.id })}>
              Practise this question
            </button>
          )}
        </div>
      </article>
    </div>
  )
}

export function QuestionBrowser({ request, onPractice, practiceDisabled, reviewDates }: {
  request: LocalRequest
} & PracticeActions) {
  const { result, retry } = useBankResource<Section[]>(request, '/api/sections')
  const [selectedSection, setSelectedSection] = useState('')

  if (result.status !== 'ready') return <LoadNotice status={result.status} resource="sections" retry={retry} />

  const sections = result.value
  const current = sections.find((section) => section.id === selectedSection) ?? sections[0]
  if (!current) return <p className="panel browser-state" role="status">No question sections are available.</p>

  return (
    <div className="question-browser">
      <div className="panel browser-section-picker">
        <label htmlFor="bank-section">Section</label>
        <select id="bank-section" value={current.id} onChange={(event) => setSelectedSection(event.target.value)}>
          {sections.map((section) => (
            <option key={section.id} value={section.id}>{section.title} ({section.question_count})</option>
          ))}
        </select>
        {onPractice && (
          <button type="button" disabled={practiceDisabled} onClick={() => onPractice({ section_id: current.id })}>
            Practise this section
          </button>
        )}
      </div>
      <SectionQuestions key={current.id} section={current} request={request}
        onPractice={onPractice} practiceDisabled={practiceDisabled} reviewDates={reviewDates} />
    </div>
  )
}
