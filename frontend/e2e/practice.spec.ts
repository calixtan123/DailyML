import { spawn } from 'node:child_process'
import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { expect, test } from '@playwright/test'

test('Two tabs share an original, reopen an offline revision and resolve an assessment dispute',
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
        await expect(tab.getByRole('article').getByText('Grading in progress')).toBeVisible()
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
      // Ensure another editable item shares the submitted question's title, regardless of random recommendations.
      const { csrf_token: csrfToken } = await (await page.request.get(`${origin}/api/session`)).json()
      const headers = { Origin: origin, 'X-CSRF-Token': csrfToken }
      const todayResponse = await page.request.post(`${origin}/api/practice/today`, { headers })
      expect(todayResponse.ok()).toBe(true)
      const today = await todayResponse.json()
      const original = today.submitted.find((item: { question: { title: string } }) => item.question.title === title)
      expect(original).toBeDefined()
      const repeated = await page.request.post(`${origin}/api/practice/items`, {
        headers, data: { question_id: original.question.id },
      })
      expect(repeated.ok()).toBe(true)
      await writeFile(gate, 'complete')
      // Read the stored result before the one reload; an inactive original does not poll in the UI.
      await expect.poll(async () => {
        const response = await page.request.get(`${origin}/api/practice/items/${original.id}/submission`)
        expect(response.ok()).toBe(true)
        return (await response.json()).request.state
      }, { timeout: 5_000, message: 'The original assessment must be stored before reloading' }).toBe('assessed')
      expect(submissionReads).toBe(readsAway)
      await page.reload()
      const session = page.getByRole('complementary', { name: 'Today’s session' })
      await expect(session.getByRole('button', { name: title })).toHaveCount(2)
      const submitted = session.getByRole('button', { name: `${title} ${original.local_date} · Assessed`, exact: true })
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
      await page.setViewportSize({ width: 1440, height: 900 })
      const readsDone = submissionReads
      await page.clock.runFor(1_200)
      expect(submissionReads).toBe(readsDone)
      const usage = await page.evaluate(async () => (await fetch('/api/usage')).json())
      expect(usage).toEqual({ attempts_24h: 1 })

      const beforeRevision = await (await page.request.post(`${origin}/api/practice/today`, { headers })).json()
      const concept = original.question.concept_id
      expect(beforeRevision.reviews[concept].due_date).toBe('2026-03-31')
      const nextDay = '2026-03-30T23:01:00+00:00'
      await writeFile(clockFile, nextDay)
      await page.clock.setSystemTime(new Date(nextDay))
      await page.reload()
      await expect(page.getByText('No practice yet today')).toBeVisible()
      const nextDayUsage = await (await page.request.get(`${origin}/api/usage`)).json()
      expect(nextDayUsage).toEqual({ attempts_24h: 0 })
      await submitted.click()
      await page.getByRole('button', { name: 'Revise answer', exact: true }).click()
      const revised = 'Use a held-out test set for the final evaluation and tune on separate data.'
      await page.getByRole('textbox', { name: 'Revised answer', exact: true }).fill(revised)
      await page.getByRole('button', { name: 'Send revised answer', exact: true }).click()

      const readSubmission = async () => {
        const response = await page.request.get(`${origin}/api/practice/items/${original.id}/submission`)
        expect(response.ok()).toBe(true)
        return response.json()
      }
      await expect.poll(async () => (await readSubmission()).revision?.request.state).toBe('assessed')
      const saved = await readSubmission()
      expect(saved.text).toBe(answer)
      expect(saved.revision.assessed_text).toBe(revised)
      expect(saved.revision.supported).toBe(true)
      const revisionUsage = await (await page.request.get(`${origin}/api/usage`)).json()
      expect(revisionUsage).toEqual({ attempts_24h: 1 })
      const afterRevision = await (await page.request.post(`${origin}/api/practice/today`, { headers })).json()
      expect(afterRevision.completion_counts).toEqual(beforeRevision.completion_counts)
      expect(afterRevision.reviews[concept]).toEqual(beforeRevision.reviews[concept])

      await page.reload()
      await submitted.click()
      const article = page.getByRole('article')
      await expect(article.getByText(revised, { exact: true })).toBeVisible()
      await expect(article.getByText('Grade 3 of 3', { exact: true })).toHaveCount(2)
      await expect(article.getByRole('button', { name: 'Revise answer', exact: true })).toHaveCount(0)

      const originalAttempt = article.getByRole('region', { name: 'Original attempt' })
      await originalAttempt.getByRole('button', { name: 'Dispute assessment' }).click()
      const objection = 'The original answer names the held-out evaluation set.'
      await originalAttempt.getByRole('textbox', { name: 'Your objection' }).fill(objection)
      await originalAttempt.getByRole('button', { name: 'Open dispute' }).click()
      await expect(article.getByText('Awaiting accepted assessment', { exact: true })).toBeVisible()
      await originalAttempt.getByRole('button', { name: 'Request reassessment' }).click()
      await expect(originalAttempt.getByText('Grade 3 of 3', { exact: true })).toHaveCount(2)
      await originalAttempt.getByRole('button', { name: 'Accept reassessment' }).click()
      await expect(originalAttempt.getByText('Reassessment accepted.')).toBeVisible()
      await expect(article.getByText('Awaiting accepted assessment', { exact: true })).toHaveCount(0)
      const afterDispute = await (await page.request.post(`${origin}/api/practice/today`, { headers })).json()
      expect(afterDispute.completion_counts).toEqual(beforeRevision.completion_counts)
      await page.reload()
      await submitted.click()
      await expect(originalAttempt.getByText(objection)).toBeVisible()
      await expect(originalAttempt.getByText('Reassessment accepted.')).toBeVisible()
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

test('A revealed offline gap is acknowledged after midnight, credited, scheduled, and restored after reload',
  async ({ page, context }) => {
    const beforeMidnight = '2026-03-29T22:58:00+00:00'
    const afterMidnight = '2026-03-29T23:01:00+00:00'
    const root = await mkdtemp(join(tmpdir(), 'daily-ml gap '))
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
      let submitRequests = 0
      await context.route(/.*/, async route => {
        const request = route.request()
        const url = new URL(request.url())
        if (url.origin !== origin) {
          await route.abort()
          return
        }
        if (url.pathname.endsWith('/submit')) submitRequests += 1
        await route.continue()
      })

      await page.clock.install({ time: new Date(beforeMidnight) })
      await page.goto(origin)
      const title = (await page.locator('#practice-question-title').textContent())!
      await expect(page.getByText('No practice yet today')).toBeVisible()
      await page.getByRole('button', { name: "I don't know" }).click()
      await expect(page.getByRole('heading', { name: 'Reference explanation' })).toBeVisible()
      await expect(page.getByRole('region', { name: 'Learning support' }).locator('.practice-support-content p').first())
        .toHaveText(/\S+/)
      await expect(page.getByRole('button', { name: 'Mark as completed' })).toBeVisible()
      expect(submitRequests).toBe(0)

      await writeFile(clockFile, afterMidnight)
      await page.clock.setSystemTime(new Date(afterMidnight))
      await page.getByRole('button', { name: 'Mark as completed' }).click()
      const practice = page.getByRole('article')
      await expect(practice.getByText('Self-reported gap')).toBeVisible()
      await expect(page.getByText('1 of 2 completed today')).toBeVisible()
      await expect(practice.getByText('Next review')).toBeVisible()
      await expect(practice.getByText('2026-03-31', { exact: true })).toBeVisible()
      expect(submitRequests).toBe(0)

      await page.reload()
      const session = page.getByRole('complementary', { name: 'Today’s session' })
      await session.getByRole('button', { name: new RegExp(`${title}.*Self-reported gap`) }).click()
      await expect(practice.getByText('Self-reported gap')).toBeVisible()
      await expect(practice.getByText("I don't know", { exact: true })).toBeVisible()
      await expect(practice.getByText('Next review')).toBeVisible()
      await expect(practice.getByText('2026-03-31', { exact: true })).toBeVisible()
      expect(submitRequests).toBe(0)
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

test('History is available before sign-in and resumes the exact saved draft with the keyboard',
  async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 })
    const beforeMidnight = '2026-03-29T22:58:00+00:00'
    const afterMidnight = '2026-03-29T23:01:00+00:00'
    const root = await mkdtemp(join(tmpdir(), 'daily-ml history '))
    const clockFile = join(root, 'clock')
    const gate = join(root, 'release-offline-grading')
    const draft = 'Keep this exact local draft while I inspect history.'
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
      await page.clock.install({ time: new Date(beforeMidnight) })
      await page.goto(origin)
      const title = (await page.locator('#practice-question-title').textContent())!
      await page.getByRole('textbox', { name: 'Your answer' }).fill(draft)
      await page.getByRole('button', { name: 'Save draft' }).click()
      await expect(page.getByText('Draft saved.', { exact: true })).toBeVisible()
      const response = await page.request.get(`${origin}/api/connection`)
      expect(response.ok()).toBeTruthy()
      const connection = await response.json()
      expect(connection.state).toBe('signed_out')

      const history = page.getByRole('link', { name: 'History', exact: true })
      await history.focus()
      await expect(history).toBeFocused()
      await page.keyboard.press('Enter')
      await expect(page).toHaveURL(`${origin}/#/history`)
      await expect(page.getByRole('heading', { name: 'History', exact: true })).toBeVisible()
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(390)
      const savedItem = page.getByRole('button', { name: title, exact: true })
      await savedItem.focus()
      await expect(savedItem).toBeFocused()
      await page.keyboard.press('Enter')
      await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible()
      await page.getByText('Saved draft', { exact: true }).click()
      await expect(page.getByRole('article', { name: title, exact: true })
        .getByText(draft, { exact: true })).toBeVisible()

      const resume = page.getByRole('button', { name: 'Resume practice', exact: true })
      await resume.focus()
      await page.keyboard.press('Enter')
      await expect(page).toHaveURL(new RegExp(`${origin.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}/#/today/[a-f0-9-]{36}$`))
      await expect(page.locator('#practice-question-title')).toHaveText(title)
      await expect(page.getByRole('textbox', { name: 'Your answer' })).toHaveValue(draft)

      await writeFile(clockFile, afterMidnight)
      await page.clock.setSystemTime(new Date(afterMidnight))
      await page.goto(origin)
      await page.getByRole('button', { name: 'Previous week' }).click()
      await expect(page.getByRole('article', { name: title, exact: true })
        .getByText(draft, { exact: true })).toBeVisible()
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
