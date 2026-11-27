import { CalendarDays, History as HistoryIcon, Layers3, Settings } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import type { Dispatch, MouseEvent, SetStateAction } from 'react'
import { request, requestFile } from './api'
import { BackupSettings } from './BackupSettings'
import { ConnectionSettings, type GradingAccount } from './ConnectionSettings'
import { GradingSettings, type GradingAction } from './GradingSettings'
import { History } from './History'
import { Today, type LeaveToday } from './Today'
import { TimezoneSettings } from './TimezoneSettings'
import type { Preferences } from './preferences'

type RetainedGradingAction = { account: string; action: GradingAction }
type Page = 'Today' | 'History' | 'Settings'

function activePage(): Page {
  if (window.location.hash === '#/settings') return 'Settings'
  if (window.location.hash === '#/history') return 'History'
  return 'Today'
}

function practiceItemInRoute(): string | undefined {
  return /^#\/today\/([a-f0-9-]{36})$/i.exec(window.location.hash)?.[1]
}

function historyIndex(): number | undefined {
  const index = window.history.state?.dailyMlRouteIndex
  return Number.isInteger(index) ? index : undefined
}

function SettingsPage({ retainedAction, setRetainedAction }: {
  retainedAction: RetainedGradingAction | undefined
  setRetainedAction: Dispatch<SetStateAction<RetainedGradingAction | undefined>>
}) {
  const [gradingAccount, setGradingAccount] = useState<GradingAccount>()
  const [preferences, setPreferences] = useState<Preferences>()
  const [connectionRevision, setConnectionRevision] = useState(0)
  const refreshGradingConnection = useCallback(() => setConnectionRevision(value => value + 1), [])

  const updatePreferences = useCallback((next: Preferences) => {
    setPreferences(previous => previous && previous.version > next.version ? previous : next)
  }, [])

  const updateGradingAccount = useCallback((account: GradingAccount) => {
    setGradingAccount(account)
    // A missing status can hide the controls without discarding the last submission.
    if (account !== undefined) {
      setRetainedAction(previous => previous?.account === account ? previous : undefined)
    }
  }, [setRetainedAction])

  function updateUncertainAction(action: GradingAction | undefined) {
    if (typeof gradingAccount !== 'string') return
    setRetainedAction(previous => {
      if (action) return { account: gradingAccount, action }
      return previous?.account === gradingAccount ? undefined : previous
    })
  }

  return (
    <>
      <h1 tabIndex={-1}>Settings</h1>
      <BackupSettings requestFile={requestFile} />
      <ConnectionSettings request={request} onGradingAccount={updateGradingAccount}
        onConnectionRestored={refreshGradingConnection} />
      <GradingSettings
        key={gradingAccount ?? null}
        request={request}
        connected={typeof gradingAccount === 'string'}
        connectionRevision={connectionRevision}
        uncertainAction={retainedAction && retainedAction.account === gradingAccount ? retainedAction.action : undefined}
        onUncertainActionChange={updateUncertainAction}
        latestPreferences={preferences}
        onPreferencesChange={updatePreferences}
      />
      <TimezoneSettings request={request} latestPreferences={preferences} onPreferencesChange={updatePreferences} />
    </>
  )
}

/** Render local practice pages, retaining unacknowledged grading submissions across navigation. */
export function App() {
  const [page, setPage] = useState(activePage)
  const [practiceItemId, setPracticeItemId] = useState(practiceItemInRoute)
  const [openedToday, setOpenedToday] = useState(() => activePage() === 'Today')
  const [retainedAction, setRetainedAction] = useState<RetainedGradingAction>()
  const content = useRef<HTMLElement>(null)
  const leaveToday = useRef<LeaveToday | null>(null)
  const route = useRef({ page, hash: window.location.hash, index: historyIndex() ?? 0 })
  const navigating = useRef(false)

  const navigate = useCallback(async (requested?: Page, itemId?: string) => {
    if (navigating.current) return
    const previous = route.current
    const next = requested ?? activePage()
    const target = requested ? `#/${next.toLowerCase()}${itemId ? `/${itemId}` : ''}` : window.location.hash
    let saved = true
    if (previous.page === 'Today' && target !== previous.hash && leaveToday.current) {
      navigating.current = true
      try {
        saved = await leaveToday.current()
      } finally {
        navigating.current = false
      }
    }

    // Prefer the latest history destination if the browser moved during the save.
    const stillAtPreviousEntry = window.location.hash === previous.hash && historyIndex() === previous.index
    if (requested && stillAtPreviousEntry) {
      if (!saved || target === previous.hash) return
      window.history.pushState({ dailyMlRouteIndex: previous.index + 1 }, '', target)
    } else if (!saved) {
      const destination = historyIndex()
      if (destination !== undefined && destination !== previous.index) {
        // Restore the accepted entry without erasing the Back or Forward destination.
        window.history.go(previous.index - destination)
      } else {
        const { pathname, search } = window.location
        window.history.replaceState({ ...window.history.state, dailyMlRouteIndex: previous.index }, '',
          `${pathname}${search}${previous.hash}`)
      }
      return
    }

    const current = activePage()
    const index = historyIndex() ?? previous.index + 1
    if (historyIndex() === undefined) {
      window.history.replaceState({ ...window.history.state, dailyMlRouteIndex: index }, '')
    }
    route.current = { page: current, hash: window.location.hash, index }
    if (current === 'Today') setOpenedToday(true)
    setPracticeItemId(practiceItemInRoute())
    setPage(current)
  }, [])

  useEffect(() => {
    window.history.replaceState({ ...window.history.state, dailyMlRouteIndex: route.current.index }, '')
    const onHashChange = () => { void navigate() }
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [navigate])

  useEffect(() => {
    document.title = `${page} · Daily ML Practice`
  }, [page])

  function navigateLink(event: MouseEvent<HTMLAnchorElement>, target: Page) {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return
    event.preventDefault()
    void navigate(target)
  }

  return (
    <>
      <a className="skip-link" href="#main-content" onClick={(event) => {
        event.preventDefault()
        const heading = content.current?.querySelector<HTMLElement>('h1:not([hidden] *)')
        if (heading) heading.focus()
        else content.current?.focus()
      }}>Skip to content</a>
      <div className="app">
        <header className="topbar">
          <div className="brand">
            <span className="logo"><Layers3 size={19} aria-hidden="true" /></span>
            <span>Daily ML Practice</span>
          </div>
        </header>
        <main id="main-content" tabIndex={-1} ref={content}>
          <div className="page-layout">
            <nav className="main-navigation" aria-label="Main navigation">
              <a className="nav-link" href="#/today" aria-current={page === 'Today' ? 'page' : undefined}
                onClick={(event) => navigateLink(event, 'Today')}>
                <CalendarDays size={17} aria-hidden="true" /> Today
              </a>
              <a className="nav-link" href="#/history" aria-current={page === 'History' ? 'page' : undefined}
                onClick={(event) => navigateLink(event, 'History')}>
                <HistoryIcon size={17} aria-hidden="true" /> History
              </a>
              <a className="nav-link" href="#/settings" aria-current={page === 'Settings' ? 'page' : undefined}
                onClick={(event) => navigateLink(event, 'Settings')}>
                <Settings size={17} aria-hidden="true" /> Settings
              </a>
            </nav>
            {openedToday && (
              <div className="today-screen" hidden={page !== 'Today'}>
                <Today request={request} active={page === 'Today'} leaveToday={leaveToday} resumeItemId={practiceItemId} />
              </div>
            )}
            {page === 'History' && <div className="page-content">
              <History request={request} onResume={itemId => void navigate('Today', itemId)} />
            </div>}
            {page === 'Settings' && (
              <div className="page-content">
                <SettingsPage retainedAction={retainedAction} setRetainedAction={setRetainedAction} />
              </div>
            )}
          </div>
        </main>
      </div>
    </>
  )
}
