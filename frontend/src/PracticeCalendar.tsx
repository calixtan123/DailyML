import { CalendarDays, ChevronLeft, ChevronRight } from 'lucide-react'

const DAY_MS = 86_400_000
const SHORT_DATE = new Intl.DateTimeFormat('en-GB', { timeZone: 'UTC', day: 'numeric', month: 'short' })
const DATE_LABEL = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'UTC', weekday: 'long', day: 'numeric', month: 'long', year: 'numeric',
})
const WEEKDAY_LABEL = new Intl.DateTimeFormat('en-GB', { timeZone: 'UTC', weekday: 'short' })

/** Navigate saved practice dates using the server's learning timezone. */
export function PracticeCalendar({ localDate, selectedDate, firstUseDate, counts, goal, onSelect }: {
  localDate: string
  selectedDate: string
  firstUseDate: string
  counts: Record<string, number>
  goal: number
  onSelect: (date: string) => void
}) {
  const selected = new Date(selectedDate)
  // ISO dates are interpreted in UTC to advance date labels without DST offsets.
  const monday = selected.getTime() - ((selected.getUTCDay() + 6) % 7) * DAY_MS
  const previousSunday = new Date(monday - DAY_MS).toISOString().slice(0, 10)
  const nextMonday = new Date(monday + 7 * DAY_MS).toISOString().slice(0, 10)
  const isToday = selectedDate === localDate
  const completed = counts[selectedDate] ?? 0
  const todayProgress = completed >= goal
    ? `${completed} completed today · Goal met`
    : `${completed} of ${goal} completed today`
  return (
    <section className="panel practice-calendar" aria-label="Weekly practice calendar">
      <div className="today-date">
        <CalendarDays size={19} aria-hidden="true" />
        <span className="calendar-range">{SHORT_DATE.formatRange(monday, monday + 6 * DAY_MS)}</span>
        <span className="muted" role="status">
          {isToday ? (completed === 0 ? 'No practice yet today' : todayProgress) : `${completed} completed`}
        </span>
        <div className="calendar-navigation">
          {!isToday && <button type="button" onClick={() => onSelect(localDate)}>Back to today</button>}
          <button type="button" aria-label="Previous week" disabled={previousSunday < firstUseDate}
            onClick={() => onSelect(previousSunday)}><ChevronLeft size={18} aria-hidden="true" /></button>
          <button type="button" aria-label="Next week" disabled={nextMonday > localDate}
            onClick={() => onSelect(nextMonday)}><ChevronRight size={18} aria-hidden="true" /></button>
        </div>
      </div>
      <ol className="practice-week">
        {Array.from({ length: 7 }, (_, index) => {
          const date = new Date(monday + index * DAY_MS)
          const iso = date.toISOString().slice(0, 10)
          const count = counts[iso] ?? 0
          const future = iso > localDate
          const beforeUse = iso < firstUseDate
          const marked = !future && !beforeUse
          const status = count >= goal ? 'Goal met' : count > 0 ? 'Partial' : 'No practice'
          const detail = marked ? `${count} completed, ${status}` : future ? 'Future date' : 'Before first use'
          return (
            <li key={iso} data-completion={marked ? status : undefined}>
              <button type="button" aria-current={iso === localDate ? 'date' : undefined}
                aria-pressed={iso === selectedDate} disabled={!marked} onClick={() => onSelect(iso)}
                aria-label={`${DATE_LABEL.format(date)}, ${detail}`}>
                <span aria-hidden="true">{WEEKDAY_LABEL.format(date)} <b>{date.getUTCDate()}</b></span>
                <strong aria-hidden="true">{marked ? count : '–'}</strong>
              </button>
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
