import { spawn, type ChildProcess } from 'node:child_process'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import { expect, test } from '@playwright/test'

async function stop(process: ChildProcess) {
  if (!process.pid || process.exitCode !== null || process.signalCode !== null) return
  await new Promise<void>((finished) => {
    const kill = setTimeout(() => process.kill('SIGKILL'), 5_000)
    process.once('exit', () => {
      clearTimeout(kill)
      finished()
    })
    process.kill('SIGINT')
  })
}

test('The installed app browses questions and preserves Settings and sign-in controls',
  async ({ page, context }) => {
    const root = await mkdtemp(join(tmpdir(), 'daily-ml browser '))
    const executable = process.env.DML_E2E_EXECUTABLE ?? resolve('../.venv/bin/daily-ml')
    const child = spawn(executable, ['run', '--no-browser'], {
      cwd: root,
      env: { ...process.env, DML_DATA_DIR: join(root, 'App data'), DML_PORT: '0' },
      stdio: ['ignore', 'ignore', 'pipe'],
    })

    try {
      const origin = await new Promise<string>((ready, reject) => {
        let output = ''
        const deadline = setTimeout(() => reject(new Error('The launcher did not report its loopback address.')), 8_000)
        child.once('error', (error) => { clearTimeout(deadline); reject(error) })
        child.once('exit', () => { clearTimeout(deadline); reject(new Error(output)) })
        child.stderr?.on('data', (chunk: Buffer) => {
          output += chunk.toString()
          const address = output.match(/http:\/\/127\.0\.0\.1:\d+/)?.[0]
          if (address) { clearTimeout(deadline); ready(address) }
        })
      })
      let authorization: URL | undefined
      await context.route(/.*/, async (route) => {
        const url = new URL(route.request().url())
        if (url.origin !== origin) {
          await route.abort()
        } else if (url.pathname.startsWith('/api/connection/authorize/')) {
          // Playwright can let redirects bypass routing. Fetch only the local response.
          const response = await route.fetch({ maxRedirects: 0 })
          const destination = response.headers().location
          if (response.status() !== 303 || !destination) {
            await route.fulfill({ response })
            return
          }
          authorization = new URL(destination)
          await route.fulfill({ contentType: 'text/html', body: '<title>Offline OAuth stand-in</title>' })
        } else {
          await route.continue()
        }
      })

      await page.goto(origin)
      await expect(page.getByRole('heading', { name: 'Browse questions' })).toBeVisible()
      await expect(page).toHaveTitle('Today · Daily ML Practice')
      const section = page.getByRole('combobox', { name: 'Section', exact: true })
      await expect(section).toHaveValue('fundamentals')
      await expect(section.getByRole('option')).toHaveCount(6)
      await page.keyboard.press('Tab')
      const skip = page.getByRole('link', { name: 'Skip to content' })
      await expect(skip).toBeFocused()
      await skip.press('Enter')
      await expect(page.getByRole('main')).toBeFocused()
      await page.keyboard.press('Tab')
      await expect(section).toBeFocused()
      await section.selectOption('deployment')
      await expect(section).toHaveValue('deployment')
      await expect(page.getByRole('heading', { name: 'Batch versus online inference' })).toBeVisible()
      await expect(page.getByText(/A shop wants to score every item overnight/)).toBeVisible()
      await expect(page.getByText(/Choose batch or online inference based on/)).toBeVisible()
      for (const width of [1440, 1024, 768, 390, 320]) {
        await page.setViewportSize({ width, height: 900 })
        expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width)
      }
      await page.setViewportSize({ width: 768, height: 900 })
      await page.evaluate(() => { document.body.style.zoom = '2' })
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(768)
      await section.selectOption('forests')
      await expect(page.getByRole('combobox', { name: 'Question', exact: true }).getByRole('option')).toHaveCount(2)
      await expect(page.getByRole('heading', { name: 'How random forests work' })).toBeVisible()
      await page.evaluate(() => { document.body.style.zoom = '1' })

      await page.getByRole('link', { name: 'Settings', exact: true }).click()
      await expect(page.getByRole('heading', { name: 'Settings', exact: true })).toBeVisible()
      await expect(page).toHaveTitle('Settings · Daily ML Practice')
      await expect(page.getByText('Signed out', { exact: true })).toBeVisible()
      for (const width of [1440, 1024, 768, 390, 320]) {
        await page.setViewportSize({ width, height: 900 })
        expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width)
      }
      await page.setViewportSize({ width: 768, height: 900 })
      await page.evaluate(() => { document.body.style.zoom = '2' })
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(768)
      await page.evaluate(() => { document.body.style.zoom = '1' })
      await page.reload()
      await expect(page.getByRole('heading', { name: 'Settings', exact: true })).toBeVisible()
      await page.keyboard.press('Tab')
      await expect(page.getByRole('link', { name: 'Skip to content' })).toBeFocused()

      const opened = context.waitForEvent('page')
      await page.getByRole('button', { name: 'Continue with ChatGPT' }).click()
      const popup = await opened
      try {
        await expect(popup).toHaveTitle('Offline OAuth stand-in')
        expect(authorization?.origin).toBe('https://auth.openai.com')
        expect(authorization?.pathname).toBe('/api/accounts/authorize')
        expect(authorization?.searchParams.get('client_id')).toBe('dynamic_agent_client')
        expect(authorization?.searchParams.get('redirect_uri')).toMatch(/^http:\/\/127\.0\.0\.1:\d+\/auth\/callback$/)
        await expect(page.getByText('Signing in', { exact: true })).toBeVisible()
        await page.getByRole('button', { name: 'Cancel sign-in' }).click()
        await expect(page.getByText('Sign-in cancelled', { exact: true })).toBeVisible()
      } finally {
        await popup.close()
      }

      const timezone = page.getByRole('textbox', { name: 'Learning timezone' })
      await expect(timezone).toHaveValue('Europe/London')
      await timezone.fill('UTC')
      await page.getByRole('button', { name: 'Save', exact: true }).click()
      await expect(page.getByText('Timezone saved.')).toBeVisible()
      await page.reload()
      await expect(timezone).toHaveValue('UTC')

      await page.getByRole('link', { name: 'Today', exact: true }).click()
      await expect(page.getByRole('heading', { name: 'Browse questions' })).toBeVisible()
      await expect(page).toHaveTitle('Today · Daily ML Practice')
    } finally {
      await stop(child)
      await rm(root, { recursive: true, force: true })
    }
  },
)
