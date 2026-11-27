import { BookOpen, Check, ChevronDown, Lightbulb } from 'lucide-react'

export type AssessedFeedback = {
  status: 'assessed'
  summary: string
  corrections: string[]
  model_answer: string
  reasoning: string[]
  example: string
  source_ids: string[]
  follow_up?: string[]
}

export function GradeScore({ grade, large = false }: { grade: number; large?: boolean }) {
  return (
    <span className={`grade-score grade-score-${grade}${large ? ' grade-score-large' : ''}`}>
      <span className="visually-hidden">Grade {grade} of 3</span>
      <span aria-hidden="true">{grade}<small>/3</small></span>
    </span>
  )
}

export function AnswerText({ label, text, open }: { label: string; text: string; open?: boolean }) {
  return (
    <details className="answer-disclosure" open={open}>
      <summary>{label}<ChevronDown size={16} aria-hidden="true" /></summary>
      <p className="practice-original">{text}</p>
    </details>
  )
}

/** Present saved assessment evidence consistently in practice, comparisons and history. */
export function Assessment({ grade, feedback, sources, title = 'Assessment', announceGrade = false }: {
  grade: number | null
  feedback: AssessedFeedback
  sources?: Record<string, string>
  title?: string
  announceGrade?: boolean
}) {
  return (
    <section className="assessment" aria-label={title}>
      <header className="assessment-header">
        {grade != null && <div role={announceGrade ? 'status' : undefined}><GradeScore grade={grade} large /></div>}
        <div><h4>{title}</h4><p>AI-generated feedback</p></div>
      </header>
      <div className="assessment-feedback">
        <section>
          <h5><Check size={16} aria-hidden="true" /> What you understood</h5>
          <p>{feedback.summary}</p>
        </section>
        <section>
          <h5><Lightbulb size={16} aria-hidden="true" /> What to improve</h5>
          {feedback.corrections.length > 0
            ? <ul>{feedback.corrections.map((point, index) => <li key={index}>{point}</li>)}</ul>
            : <p>No corrections recorded.</p>}
        </section>
      </div>
      <section className="assessment-model">
        <h5><BookOpen size={17} aria-hidden="true" /> Model answer</h5>
        <p>{feedback.model_answer}</p>
      </section>
      <details className="answer-disclosure">
        <summary>Reasoning, example and sources<ChevronDown size={16} aria-hidden="true" /></summary>
        <div>
          <ol>{feedback.reasoning.map((step, index) => <li key={index}>{step}</li>)}</ol>
          <p>{feedback.example}</p>
          {feedback.source_ids.map(id => {
            const source = sources?.[id]
            return source?.startsWith('https://') && <p key={id}><a href={source} target="_blank" rel="noreferrer">{id}</a></p>
          })}
          {feedback.follow_up?.map((point, index) => <p key={index}>{point}</p>)}
        </div>
      </details>
    </section>
  )
}
