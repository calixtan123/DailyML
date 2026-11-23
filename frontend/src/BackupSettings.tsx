import { useState } from 'react'
import type { LocalFileRequest } from './api'

const BACKUP = '/api/backup'

/** Download a verified, credential-free local learning backup from Settings. */
export function BackupSettings({ requestFile }: { requestFile: LocalFileRequest }) {
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')

  async function download() {
    setBusy(true)
    setMessage('')
    try {
      const blob = await requestFile(BACKUP, { method: 'POST' })
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = 'daily-ml-learning-backup.sqlite3'
      link.click()
      URL.revokeObjectURL(url)
      setMessage('Backup downloaded.')
    } catch (failure) {
      setMessage(failure instanceof Error ? failure.message : 'Could not create a backup.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="panel" aria-labelledby="backup-heading">
      <h2 id="backup-heading">Back up learning data</h2>
      <div className="settings-row">
        <div>
          <p>Download a verified copy of your local learning records, question snapshots and editable preferences.</p>
          <p className="muted">Credentials and ChatGPT tokens are not included. To restore, stop Daily ML Practice before running the documented restore command.</p>
        </div>
        <div className="actions"><button className="primary" disabled={busy} onClick={() => void download()}>
          {busy ? 'Creating backup…' : 'Back up learning data'}
        </button></div>
      </div>
      {message && <p className={message === 'Backup downloaded.' ? 'notice' : 'error'} role="status">{message}</p>}
    </section>
  )
}
