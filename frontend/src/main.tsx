import { BookOpen, Settings } from 'lucide-react'
import { useState } from 'react'
import { GradingSettings } from './GradingSettings'
import { createRoot } from 'react-dom/client'
import { request } from './api'
import { ConnectionSettings } from './ConnectionSettings'
import { TimezoneSettings } from './TimezoneSettings'
import './styles.css'

function App() {
  const [gradingAccount, setGradingAccount] = useState<string | null>(null)
  return (
    <>
      <a className="skip-link" href="#main-content">Skip to content</a>
      <div className="app">
        <aside className="sidebar">
          <div className="brand">
            <span className="logo"><BookOpen size={20} aria-hidden="true" /></span>
            <span>Daily ML Practice</span>
          </div>
          <nav aria-label="Main navigation">
            <a className="nav-link" href="#/settings" aria-current="page">
              <Settings size={17} aria-hidden="true" /> Settings
            </a>
          </nav>
        </aside>
        <div className="workspace">
          <header className="topbar"><span>Daily ML Practice</span><span aria-hidden="true">/</span> Settings</header>
          <main id="main-content" tabIndex={-1}>
            <h1>Settings</h1>
            <ConnectionSettings request={request} onGradingAccount={setGradingAccount} />
            <GradingSettings key={gradingAccount} request={request} connected={gradingAccount !== null} />
            <TimezoneSettings request={request} />
          </main>
        </div>
      </div>
    </>
  )
}

const root = document.getElementById('root')
if (root) createRoot(root).render(<App />)
