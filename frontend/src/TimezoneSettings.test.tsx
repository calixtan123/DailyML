import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { LocalApiError, type LocalRequest } from './api'
import type { Preferences } from './preferences'
import { TimezoneSettings } from './TimezoneSettings'

afterEach(cleanup)

describe('learning preferences', () => {
  it('shows precedence and effective settings even when account models cannot be loaded', async () => {
    const request: LocalRequest = async <T,>() => ({
      timezone: 'Europe/London', version: 1, source: 'installation',
      model_id: 'configured-model', model_source: 'environment',
    }) as T
    render(<TimezoneSettings request={request} />)
    const zone = await screen.findByRole('textbox', { name: 'Learning timezone' })
    expect(zone).toHaveProperty('readOnly', true)
    expect(screen.getByText(/Installation TOML.*DML_TIMEZONE/i)).toBeTruthy()
    expect(screen.getByText('configured-model')).toBeTruthy()
    expect(screen.getByText(/Environment override.*DML_MODEL_ID/i)).toBeTruthy()
    expect(screen.getAllByRole('listitem').map(item => item.textContent)).toEqual([
      'Packaged defaults', 'Saved SQLite preferences', 'Installation TOML overrides', 'Environment overrides',
    ])
  })

  it('focuses a stale write error and retains the edit after an explicit reload', async () => {
    let timezone = 'Europe/London'
    let version = 0
    const writes: unknown[] = []
    const request: LocalRequest = async <T,>(_path: string, options?: RequestInit) => {
      if (options?.method === 'PUT') {
        const edit = JSON.parse(String(options.body))
        writes.push(edit)
        if (edit.version !== version) throw new LocalApiError(409, { detail: 'Preferences changed in another tab.' })
        timezone = edit.timezone
        version++
      }
      return { timezone, version, source: 'preferences', model_id: null, model_source: 'default' } as T
    }
    render(<TimezoneSettings request={request} />)
    const zone = await screen.findByRole('textbox', { name: 'Learning timezone' })
    fireEvent.change(zone, { target: { value: 'Asia/Tokyo' } })
    timezone = 'UTC'
    version = 1
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    const error = await screen.findByRole('alert')
    expect(document.activeElement).toBe(error)
    expect(zone).toHaveProperty('value', 'Asia/Tokyo')
    expect(screen.getByRole('button', { name: 'Save' })).toHaveProperty('disabled', true)

    const reload = screen.getByRole('button', { name: 'Reload preferences' })
    reload.focus()
    fireEvent.click(reload)
    await screen.findByText(/Saved timezone is UTC/)
    expect(zone).toHaveProperty('value', 'Asia/Tokyo')
    expect(document.activeElement).toBe(zone)
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await screen.findByText('Timezone saved.')
    expect(writes).toEqual([{ timezone: 'Asia/Tokyo', version: 0 }, { timezone: 'Asia/Tokyo', version: 1 }])
  })

  it('waits for the initial preferences read before allowing edits to a catalog-provided timezone', async () => {
    const fromCatalog: Preferences = {
      timezone: 'Europe/London', version: 1, source: 'preferences', model_id: 'model-z', model_source: 'preferences',
    }
    const newerSettings = { ...fromCatalog, timezone: 'UTC', version: 2 }
    let completeRead!: (preferences: Preferences) => void
    const pendingRead = new Promise<Preferences>(resolve => { completeRead = resolve })
    const request = vi.fn(async (_path: string, options?: RequestInit) => {
      if (options?.method === 'PUT') return { ...newerSettings, timezone: 'Asia/Tokyo', version: 3 }
      return pendingRead
    })
    render(<TimezoneSettings request={request as LocalRequest} latestPreferences={fromCatalog} />)
    const zone = screen.getByRole('textbox', { name: 'Learning timezone' })
    expect(zone).toHaveProperty('value', 'Europe/London')
    expect(zone).toHaveProperty('readOnly', true)

    completeRead(newerSettings)
    await screen.findByDisplayValue('UTC')
    expect(zone).toHaveProperty('readOnly', false)
    fireEvent.change(zone, { target: { value: 'Asia/Tokyo' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))
    await screen.findByText('Timezone saved.')
    expect(JSON.parse(String(request.mock.calls[1][1]?.body))).toEqual({ timezone: 'Asia/Tokyo', version: 2 })
  })
})
