import { BookOpen, Settings } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import type { Dispatch, MouseEvent, SetStateAction } from 'react'
import { request } from './api'
import { ConnectionSettings, type GradingAccount } from './ConnectionSettings'
import { GradingSettings, type GradingAction } from './GradingSettings'
import { Today, type LeaveToday } from './Today'
import { TimezoneSettings } from './TimezoneSettings'

type RetainedGradingAction = { account: string; action: GradingAction }
type Page = 'Today' | 'Settings'

function activePage(): Page {
  return window.location.hash === '#/settings' ? 'Settings' : 'Today'
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
      <h1>Settings</h1>
      <ConnectionSettings request={request} onGradingAccount={updateGradingAccount} />
      <GradingSettings
        key={gradingAccount ?? null}
        request={request}
        connected={typeof gradingAccount === 'string'}
        uncertainAction={retainedAction && retainedAction.account === gradingAccount ? retainedAction.action : undefined}
        onUncertainActionChange={updateUncertainAction}
      />
      <TimezoneSettings request={request} />
    </>
  )
}

/** Render Today and Settings, retaining unacknowledged grading submissions across navigation. */
export function App() {
  const [page, setPage] = useState(activePage)
  const [openedToday, setOpenedToday] = useState(() => activePage() === 'Today')
  const [retainedAction, setRetainedAction] = useState<RetainedGradingAction>()
  const content = useRef<HTMLElement>(null)
  const leaveToday = useRef<LeaveToday | null>(null)
  const route = useRef({ page, hash: window.location.hash, index: historyIndex() ?? 0 })
  const navigating = useRef(false)

  const navigate = useCallback(async (requested?: Page) => {
    if (navigating.current) return
    const previous = route.current
    const next = requested ?? activePage()
    let saved = true
    if (previous.page === 'Today' && next === 'Settings' && leaveToday.current) {
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
      const target = `#/${next.toLowerCase()}`
      if (!saved || target === previous.hash) return
      window.history.pushState({ dailyMlRouteIndex: previous.index + 1 }, '', target)
    } else if (!saved && activePage() === 'Settings') {
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
        content.current?.focus()
      }}>Skip to content</a>
      <div className="app">
        <aside className="sidebar">
          <div className="brand">
            <span className="logo"><BookOpen size={20} aria-hidden="true" /></span>
            <span>Daily ML Practice</span>
          </div>
          <nav className="main-navigation" aria-label="Main navigation">
            <a className="nav-link" href="#/today" aria-current={page === 'Today' ? 'page' : undefined}
              onClick={(event) => navigateLink(event, 'Today')}>
              <BookOpen size={17} aria-hidden="true" /> Today
            </a>
            <a className="nav-link" href="#/settings" aria-current={page === 'Settings' ? 'page' : undefined}
              onClick={(event) => navigateLink(event, 'Settings')}>
              <Settings size={17} aria-hidden="true" /> Settings
            </a>
          </nav>
        </aside>
        <div className="workspace">
          <header className="topbar"><span>Daily ML Practice</span><span aria-hidden="true">/</span> {page}</header>
          <main id="main-content" tabIndex={-1} ref={content}>
            {openedToday && (
              <div hidden={page !== 'Today'}>
                <Today request={request} active={page === 'Today'} leaveToday={leaveToday} />
              </div>
            )}
            {page === 'Settings' && (
              <SettingsPage retainedAction={retainedAction} setRetainedAction={setRetainedAction} />
            )}
          </main>
        </div>
      </div>
    </>
  )
}
