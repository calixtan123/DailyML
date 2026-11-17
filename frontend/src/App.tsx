import { BookOpen, Settings } from 'lucide-react'
import { useCallback, useEffect, useRef, useState } from 'react'
import type { Dispatch, SetStateAction } from 'react'
import { request } from './api'
import { ConnectionSettings, type GradingAccount } from './ConnectionSettings'
import { GradingSettings, type GradingAction } from './GradingSettings'
import { QuestionBrowser } from './QuestionBrowser'
import { TimezoneSettings } from './TimezoneSettings'

type RetainedGradingAction = { account: string; action: GradingAction }

function activePage() {
  return window.location.hash === '#/settings' ? 'Settings' : 'Today'
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
  const [retainedAction, setRetainedAction] = useState<RetainedGradingAction>()
  const content = useRef<HTMLElement>(null)

  useEffect(() => {
    const onHashChange = () => setPage(activePage())
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])

  useEffect(() => {
    document.title = `${page} · Daily ML Practice`
  }, [page])

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
            <a className="nav-link" href="#/today" aria-current={page === 'Today' ? 'page' : undefined}>
              <BookOpen size={17} aria-hidden="true" /> Today
            </a>
            <a className="nav-link" href="#/settings" aria-current={page === 'Settings' ? 'page' : undefined}>
              <Settings size={17} aria-hidden="true" /> Settings
            </a>
          </nav>
        </aside>
        <div className="workspace">
          <header className="topbar"><span>Daily ML Practice</span><span aria-hidden="true">/</span> {page}</header>
          <main id="main-content" tabIndex={-1} ref={content}>
            {page === 'Today' ? (
              <>
                <h1>Browse questions</h1>
                <QuestionBrowser request={request} />
              </>
            ) : (
              <SettingsPage retainedAction={retainedAction} setRetainedAction={setRetainedAction} />
            )}
          </main>
        </div>
      </div>
    </>
  )
}
