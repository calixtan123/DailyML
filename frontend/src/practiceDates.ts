const PRACTICE_DATE_FORMAT = new Intl.DateTimeFormat('en-GB', {
  timeZone: 'UTC', weekday: 'long', day: 'numeric', month: 'long', year: 'numeric',
})

export function formatPracticeDate(date: string) {
  return PRACTICE_DATE_FORMAT.format(new Date(date))
}

export function nextPracticeDate(date: string) {
  const next = new Date(date)
  next.setUTCDate(next.getUTCDate() + 1)
  return next.toISOString().slice(0, 10)
}
