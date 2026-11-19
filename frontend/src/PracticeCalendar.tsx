import { CalendarDays } from 'lucide-react'

const DAY_MS = 86_400_000
const TODAY_LABEL = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'UTC', weekday: 'long', day: 'numeric', month: 'long',
})
const DATE_LABEL = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'UTC', weekday: 'long', day: 'numeric', month: 'long', year: 'numeric',
})
const WEEKDAY_LABEL = new Intl.DateTimeFormat('en-GB', { timeZone: 'UTC', weekday: 'short' })

/** Display the week containing the server's current learning date. */
export function PracticeCalendar({ localDate, firstUseDate, counts, goal }: {
  localDate: string
  firstUseDate: string
  counts: Record<string, number>
  goal: number
}) {
  const today = new Date(localDate)
  // ISO dates are interpreted in UTC to advance date labels without DST offsets.
  const monday = today.getTime() - ((today.getUTCDay() + 6) % 7) * DAY_MS
  const completed = counts[localDate] ?? 0
  const todayProgress = completed >= goal
    ? `${completed} completed today · Goal met`
    : `${completed} of ${goal} completed today`
  return (
    <section className="panel practice-calendar" aria-label="Weekly practice calendar">
      <div className="today-date">
        <CalendarDays size={19} aria-hidden="true" />
        <time dateTime={localDate}>{TODAY_LABEL.format(today)}</time>
        <span className="muted" role="status">
          {completed === 0 ? 'No practice yet today' : todayProgress}
        </span>
      </div>
      <ol className="practice-week">
        {Array.from({ length: 7 }, (_, index) => {
          const date = new Date(monday + index * DAY_MS)
          const iso = date.toISOString().slice(0, 10)
          const count = counts[iso] ?? 0
          const future = iso > localDate
          const beforeUse = iso < firstUseDate
          const marked = !future && !beforeUse
          const status = count >= goal ? 'Goal met' : count === 1 ? 'Partial' : 'No practice'
          const detail = marked ? `${count} completed, ${status}` : future ? 'Future date' : 'Before first use'
          return (
            <li key={iso} data-completion={marked ? status : undefined}>
              <time dateTime={iso} aria-current={iso === localDate ? 'date' : undefined}
                aria-label={`${DATE_LABEL.format(date)}, ${detail}`}>
                <span aria-hidden="true">{WEEKDAY_LABEL.format(date)}</span>
                <strong aria-hidden="true">{marked ? count : '–'}</strong>
              </time>
            </li>
          )
        })}
      </ol>
      <ul className="practice-legend" role="list" aria-label="Calendar legend">
        <li>0 No practice</li>
        <li>1 Partial</li>
        <li>{goal}+ Goal met</li>
      </ul>
    </section>
  )
}
