import { BookOpen, Settings } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { request } from './api'
import { ConnectionSettings } from './ConnectionSettings'
import { QuestionBrowser } from './QuestionBrowser'
import { TimezoneSettings } from './TimezoneSettings'

function activePage() {
  return window.location.hash === '#/settings' ? 'Settings' : 'Today'
}

export function App() {
  const [page, setPage] = useState(activePage)
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
              <>
                <h1>Settings</h1>
                <ConnectionSettings request={request} />
                <TimezoneSettings request={request} />
              </>
            )}
          </main>
        </div>
      </div>
    </>
  )
}
