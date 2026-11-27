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

test('The installed app saves practice drafts and preserves Settings and sign-in controls',
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
      await expect(page.getByRole('heading', { name: 'Today', exact: true })).toBeVisible()
      await expect(page).toHaveTitle('Today · Daily ML Practice')
      await page.keyboard.press('Tab')
      const skip = page.getByRole('link', { name: 'Skip to content' })
      await expect(skip).toBeFocused()
      await skip.press('Enter')
      await expect(page.getByRole('heading', { name: 'Today', exact: true })).toBeFocused()

      await page.setViewportSize({ width: 1440, height: 900 })
      const session = page.getByRole('complementary', { name: 'Today’s session' })
      const items = session.getByRole('list').getByRole('button')
      await expect(items).toHaveCount(2)
      const pair = await items.allTextContents()
      const editor = page.getByRole('textbox', { name: 'Your answer' })
      const practiceTitle = page.locator('#practice-question-title')
      const firstQuestion = await practiceTitle.textContent()
      const secondTab = await context.newPage()
      await secondTab.goto(origin)
      await expect(secondTab.getByRole('textbox', { name: 'Your answer' })).toBeVisible()
      await expect(secondTab.locator('#practice-question-title')).toHaveText(firstQuestion ?? '')
      await secondTab.setViewportSize({ width: 1440, height: 900 })
      expect(await secondTab.getByRole('complementary', { name: 'Today’s session' })
        .getByRole('list').getByRole('button').allTextContents()).toEqual(pair)

      const saveRoute = '**/api/practice/items/*/draft'
      await page.route(saveRoute, (route) => route.fulfill({
        status: 503, json: { detail: 'Simulated local save failure.' },
      }))
      await editor.fill('Keep my edit until the save succeeds.')
      await items.nth(1).click()
      await expect(page.getByRole('alert')).toHaveText('Could not save your draft. Your text is still here.')
      await expect(page.getByRole('alert')).toBeFocused()
      await expect(editor).toHaveValue('Keep my edit until the save succeeds.')
      await expect(practiceTitle).toHaveText(firstQuestion ?? '')
      await page.unroute(saveRoute)
      await items.nth(1).click()
      await expect(practiceTitle).not.toHaveText(firstQuestion ?? '')
      await expect(practiceTitle).toBeFocused()

      const otherEditor = secondTab.getByRole('textbox', { name: 'Your answer' })
      await otherEditor.fill('My stale tab’s edit.')
      await secondTab.getByRole('button', { name: 'Save draft' }).click()
      await expect(secondTab.getByRole('textbox', { name: 'Saved in another tab' }))
        .toHaveValue('Keep my edit until the save succeeds.')
      await expect(secondTab.getByRole('alert')).toBeFocused()
      await expect(otherEditor).toHaveValue('My stale tab’s edit.')
      await secondTab.getByRole('button', { name: 'Use saved version' }).click()
      await expect(otherEditor).toHaveValue('Keep my edit until the save succeeds.')
      await secondTab.close()
      await page.reload()
      await expect(editor).toHaveValue('Keep my edit until the save succeeds.')

      await page.getByRole('link', { name: 'Settings', exact: true }).click()
      await expect(page).toHaveTitle('Settings · Daily ML Practice')
      await page.getByRole('link', { name: 'Today', exact: true }).click()
      await editor.fill('Save this answer before moving through browser history.')
      await page.route(saveRoute, (route) => route.fulfill({
        status: 503, json: { detail: 'Simulated local save failure.' },
      }))
      await page.goBack()
      await expect(page.getByRole('alert')).toHaveText('Could not save your draft. Your text is still here.')
      await expect(page).toHaveURL(`${origin}/#/today`)
      await expect(editor).toHaveValue('Save this answer before moving through browser history.')

      await page.unroute(saveRoute)
      await page.goBack()
      await expect(page).toHaveTitle('Settings · Daily ML Practice')
      await page.goForward()
      await expect(page).toHaveTitle('Today · Daily ML Practice')
      await page.reload()
      await expect(editor).toHaveValue('Save this answer before moving through browser history.')

      const compact = page.getByRole('combobox', { name: 'Session question', exact: true })
      const mainNavigation = page.getByRole('navigation', { name: 'Main navigation' })
      const questionCard = page.locator('.today-question')
      for (const width of [1440, 1024, 768]) {
        await page.setViewportSize({ width, height: 900 })
        await expect(compact).toBeHidden()
        const [left, center, right] = await Promise.all([
          mainNavigation.boundingBox(), questionCard.boundingBox(), session.boundingBox(),
        ])
        if (!left || !center || !right) throw new Error('The three practice columns must be visible.')
        expect(left.x + left.width).toBeLessThanOrEqual(center.x)
        expect(center.x + center.width).toBeLessThanOrEqual(right.x)
        expect(Math.max(left.y, center.y, right.y)).toBeLessThan(
          Math.min(left.y + left.height, center.y + center.height, right.y + right.height),
        )
        expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width)
        await expect(page.getByRole('button', { name: 'More questions' })).toBeVisible()
      }
      for (const width of [390, 320]) {
        await page.setViewportSize({ width, height: 900 })
        await expect(compact).toBeVisible()
        expect((await compact.boundingBox())?.height).toBeGreaterThanOrEqual(44)
        expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width)
        await expect(page.getByRole('button', { name: 'More questions' })).toBeVisible()
      }
      await page.setViewportSize({ width: 390, height: 900 })
      await page.getByRole('button', { name: 'More questions' }).click()
      await expect(compact.getByRole('option')).toHaveCount(3)

      const browse = page.locator('.today-bank > summary')
      await browse.focus()
      await page.keyboard.press('Enter')
      const section = page.getByRole('combobox', { name: 'Section', exact: true })
      await expect(section).toHaveValue('fundamentals')
      await expect(section.getByRole('option')).toHaveCount(6)
      await expect(browse).toBeFocused()
      await page.keyboard.press('Tab')
      await expect(section).toBeFocused()
      await section.selectOption('deployment')
      await expect(section).toHaveValue('deployment')
      await expect(page.locator('#bank-question-title')).toHaveText('Batch versus online inference')
      await expect(page.locator('.browser-question').getByText(/A shop wants to score every item overnight/)).toBeVisible()
      await expect(page.locator('.browser-question').getByText(/Choose batch or online inference based on/)).toBeVisible()
      for (const width of [1440, 1024, 768, 390, 320]) {
        await page.setViewportSize({ width, height: 900 })
        expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width)
      }
      await page.setViewportSize({ width: 768, height: 900 })
      await page.evaluate(() => { document.body.style.zoom = '2' })
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(768)
      await section.selectOption('forests')
      await expect(page.getByRole('combobox', { name: 'Question', exact: true }).getByRole('option')).toHaveCount(2)
      await expect(page.locator('#bank-question-title')).toHaveText('How random forests work')
      await page.evaluate(() => { document.body.style.zoom = '1' })
      await page.getByRole('button', { name: 'Practise this question' }).click()
      await expect(practiceTitle).toHaveText('How random forests work')

      await page.getByRole('link', { name: 'Settings', exact: true }).click()
      await expect(page.getByRole('heading', { name: 'Settings', exact: true })).toBeVisible()
      await expect(page).toHaveTitle('Settings · Daily ML Practice')
      await expect(page.getByText('Signed out', { exact: true })).toBeVisible()
      await expect(page.getByText('No model selected')).toBeVisible()
      await expect(page.getByText('Configuration order')).toBeVisible()
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

      const oldModel = `retired-${'long-model-id-'.repeat(12)}old`
      const replacement = { slug: 'available-model', display_name: 'Available model' }
      let models = [{ slug: oldModel, display_name: 'Retired model' }, replacement]
      let selected = oldModel
      let version = 1
      const writes: { model_id: string; version: number }[] = []
      const currentSettings = () => ({ timezone: 'UTC', source: 'preferences', version,
        model_id: selected, model_source: 'preferences' })
      const selection = () => ({ selected, version, overridden: false, source: 'preferences', settings: currentSettings() })

      await page.route('**/api/connection', route => route.fulfill({ json: {
        state: 'connected', email: 'offline@example.test', plan_authorized: true, access_token_usable: true,
        reauthorization_required: false, message: null, remote_revocation_unconfirmed: false, requires_consent: false,
      } }))
      await page.route('**/api/settings', route => route.fulfill({ json: currentSettings() }))
      await page.route('**/api/connection/models', route => route.fulfill({ json: { models, ...selection() } }))
      await page.route('**/api/connection/model', route => {
        const edit = route.request().postDataJSON()
        writes.push(edit)
        if (writes.length === 1) {
          version++
          return route.fulfill({ status: 409, json: { detail: 'Preferences changed in another tab.' } })
        }
        selected = edit.model_id
        version++
        return route.fulfill({ json: selection() })
      })

      await page.setViewportSize({ width: 320, height: 820 })
      await page.reload()
      const model = page.getByRole('combobox', { name: 'Grading model' })
      const refresh = page.getByRole('button', { name: 'Refresh model catalog' })
      const saveModel = page.getByRole('button', { name: 'Save model' })
      const alert = page.getByRole('alert')
      const run = page.getByRole('button', { name: 'Run connection test' })
      await expect(model).toHaveValue(oldModel)
      models = [replacement]
      await refresh.press('Enter')
      await expect(alert).toContainText('is unavailable in this account.')
      await expect(refresh).toBeFocused()
      await expect(run).toBeDisabled()
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(320)
      await page.keyboard.press('Tab')
      await expect(model).toBeFocused()
      await model.selectOption(replacement.slug)
      expect(writes).toEqual([])
      await page.keyboard.press('Tab')
      await expect(saveModel).toBeFocused()
      await page.keyboard.press('Enter')
      await expect(alert).toHaveText('Preferences changed in another tab.')
      await expect(alert).toBeFocused()
      await expect(saveModel).toBeDisabled()

      await refresh.press('Enter')
      await expect(model).toHaveValue('')
      await model.selectOption(replacement.slug)
      await saveModel.press('Enter')
      await expect(page.getByText('Model saved.')).toBeVisible()
      await expect(page.getByText(replacement.slug, { exact: true })).toBeVisible()
      await expect(run).toBeEnabled()
      await expect(page.getByText('Model attempts in the last 24 hours: 0')).toBeVisible()
      expect(writes).toEqual([{ model_id: replacement.slug, version: 1 }, { model_id: replacement.slug, version: 2 }])

      await page.getByRole('link', { name: 'Today', exact: true }).click()
      await expect(page.getByRole('heading', { name: 'Today', exact: true })).toBeVisible()
      await expect(page).toHaveTitle('Today · Daily ML Practice')
    } finally {
      await stop(child)
      await rm(root, { recursive: true, force: true })
    }
  },
)
