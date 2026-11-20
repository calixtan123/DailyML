import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { BackupSettings } from './BackupSettings'

afterEach(() => vi.unstubAllGlobals())

it('offers a local SQLite download and explains the stopped-app restore boundary', async () => {
  const requestFile = vi.fn().mockResolvedValue(new Blob(['SQLite format 3'], { type: 'application/vnd.sqlite3' }))
  const create = vi.fn(() => 'blob:backup')
  const revoke = vi.fn()
  vi.stubGlobal('URL', { createObjectURL: create, revokeObjectURL: revoke })
  render(<BackupSettings requestFile={requestFile} />)

  expect(screen.getByText(/credentials and ChatGPT tokens are not included/i)).toBeTruthy()
  expect(screen.getByText(/documented restore command/i)).toBeTruthy()
  fireEvent.click(screen.getByRole('button', { name: 'Back up learning data' }))

  await waitFor(() => expect(requestFile).toHaveBeenCalledWith('/api/backup', { method: 'POST' }))
  expect(await screen.findByText('Backup downloaded.')).toBeTruthy()
  expect(create).toHaveBeenCalled()
  expect(revoke).toHaveBeenCalledWith('blob:backup')
})
