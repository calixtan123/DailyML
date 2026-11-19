import { spawn } from 'node:child_process'
import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { expect, test } from '@playwright/test'

test('Two tabs complete one saved original after London midnight and reopen feedback offline',
  async ({ page, context }) => {
    const beforeMidnight = '2026-03-29T22:58:00+00:00'
    const afterMidnight = '2026-03-29T23:01:00+00:00'
    const answer = 'Keep the evaluation data held back from every tuning step.'
    const root = await mkdtemp(join(tmpdir(), 'daily-ml submission '))
    const clockFile = join(root, 'clock')
    const gate = join(root, 'release-offline-grading')
    await writeFile(clockFile, beforeMidnight)
    const executable = process.env.DML_E2E_EXECUTABLE ?? resolve('../.venv/bin/daily-ml')
    const child = spawn(join(dirname(executable), 'python'), [resolve('e2e/practice_server.py')], {
      cwd: root,
      env: {
        ...process.env, DML_DATA_DIR: join(root, 'App data'), DML_PORT: '0', DML_TIMEZONE: 'Europe/London',
        DML_E2E_CLOCK: clockFile, DML_E2E_GRADING_GATE: gate,
      },
      stdio: ['ignore', 'ignore', 'pipe'],
    })

    try {
      const origin = await new Promise<string>((ready, reject) => {
        let output = ''
        const deadline = setTimeout(() => reject(new Error(`Test launcher did not start. ${output}`)), 8_000)
        child.once('error', (error) => { clearTimeout(deadline); reject(error) })
        child.once('exit', () => { clearTimeout(deadline); reject(new Error(output)) })
        child.stderr?.on('data', (chunk: Buffer) => {
          output += chunk.toString()
          const address = output.match(/http:\/\/127\.0\.0\.1:\d+/)?.[0]
          if (address) { clearTimeout(deadline); ready(address) }
        })
      })
      const actionKeys = new Set<string>()
      let submissionReads = 0
      await context.route(/.*/, async route => {
        const request = route.request()
        const url = new URL(request.url())
        if (url.origin !== origin) {
          await route.abort()
          return
        }
        if (url.pathname.endsWith('/submit')) actionKeys.add(request.postDataJSON().idempotency_key)
        if (url.pathname.endsWith('/submission')) submissionReads += 1
        await route.continue()
      })

      const second = await context.newPage()
      for (const tab of [page, second]) {
        await tab.clock.install({ time: new Date(beforeMidnight) })
        await tab.goto(origin)
        await expect(tab.getByText('No practice yet today')).toBeVisible()
        await tab.getByRole('textbox', { name: 'Your answer' }).fill(answer)
      }
      const title = (await page.locator('#practice-question-title').textContent())!
      await expect(second.locator('#practice-question-title')).toHaveText(title)
      await writeFile(clockFile, afterMidnight)
      for (const tab of [page, second]) await tab.clock.setSystemTime(new Date(afterMidnight))
      await Promise.all([page, second].map(tab => tab.getByRole('button', { name: 'Submit answer' }).click()))
      expect(actionKeys.size).toBe(2)
      for (const tab of [page, second]) {
        await expect(tab.getByText('1 of 2 completed today')).toBeVisible()
        await expect(tab.getByText('Grading in progress')).toBeVisible()
        await expect(tab.getByText('2026-03-30', { exact: true })).toBeVisible()
        await expect(tab.getByText(answer, { exact: true })).toBeVisible()
      }
      await second.close()

      await page.getByRole('button', { name: 'More questions' }).click()
      await expect(page.getByRole('textbox', { name: 'Your answer' })).toBeEnabled()
      await expect(page.getByText('1 of 2 completed today')).toBeVisible()
      const readsAway = submissionReads
      await page.clock.runFor(1_200)
      expect(submissionReads).toBe(readsAway)
      await writeFile(gate, 'complete')
      await page.reload()
      const submitted = page.getByRole('complementary', { name: 'Today’s session' })
        .getByRole('button', { name: title, exact: true })
      await submitted.click()
      await expect(page.getByText('Grade 3 of 3')).toBeVisible()
      await expect(page.getByText('Give a concrete example.')).toBeVisible()
      await page.getByText('Reasoning, example and sources').click()
      await expect(page.getByText('Apply the principle to a new dataset.')).toBeVisible()
      await page.getByText('Original answer', { exact: true }).click()
      await expect(page.getByText(answer, { exact: true })).toBeVisible()
      for (const width of [1440, 768, 390, 320]) {
        await page.setViewportSize({ width, height: 900 })
        expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width)
      }
      const readsDone = submissionReads
      await page.clock.runFor(1_200)
      expect(submissionReads).toBe(readsDone)
      const usage = await page.evaluate(async () => (await fetch('/api/usage')).json())
      expect(usage).toEqual({ attempts_24h: 1 })
    } finally {
      await writeFile(gate, 'cleanup')
      if (child.pid && child.exitCode === null && child.signalCode === null) {
        await new Promise<void>(done => {
          const kill = setTimeout(() => child.kill('SIGKILL'), 5_000)
          child.once('exit', () => { clearTimeout(kill); done() })
          child.kill('SIGINT')
        })
      }
      await rm(root, { recursive: true, force: true })
    }
  },
)
