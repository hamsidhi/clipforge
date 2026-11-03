// Screenshot every page of the running web app (clipforge ui) and report console errors.
// Usage: node scripts/screenshots.mjs [baseUrl] [jobId] [clipId]
// Uses the locally installed Microsoft Edge / Chrome through playwright-core (no browser download).
import { createRequire } from 'node:module'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const require = createRequire(path.join(path.dirname(fileURLToPath(import.meta.url)), '../frontend/package.json'))
const { chromium } = require('playwright-core')

const base = process.argv[2] ?? 'http://127.0.0.1:8000'
const job = process.argv[3] ?? '1'
const clip = process.argv[4] ?? `${job}-1`
const set = process.argv[5] ?? 'main'
const out = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'docs', 'screenshots', ...(set === 'phase12' ? ['phase12'] : []))

const PHASE12 = [
  ['01-channels', '/channels'],
  ['02-schedule-week', '/schedule'],
  ['03-batches', '/batches'],
  ['04-dashboard', '/dashboard'],
  ['05-tracker', '/tracker'],
  ['06-settings-publishing', '/settings#publishing'],
  ['07-gallery-add-to-schedule', `/jobs/${job}/clips`],
]
const MAIN = [
  ['01-dashboard', '/'],
  ['02-new-job', '/new'],
  ['03-job-progress', `/jobs/${job}`],
  ['04-gallery', `/jobs/${job}/clips`],
  ['05-clip-editor', `/clips/${clip}/edit`],
  ['06-publish-kit', `/clips/${clip}/publish`],
  ['07-tracker', '/tracker'],
  ['08-settings', '/settings'],
  ['09-system-check', '/system'],
]
const pages = set === 'phase12' ? PHASE12 : MAIN
const mobile = set === 'phase12' ? ['01-channels', '02-schedule-week', '04-dashboard', '03-batches']
  : ['01-dashboard', '04-gallery', '05-clip-editor', '02-new-job']

let browser
for (const channel of ['msedge', 'chrome']) {
  try {
    browser = await chromium.launch({ channel })
    break
  } catch { /* try next */ }
}
if (!browser) throw new Error('No Edge/Chrome found for screenshots')

let errors = 0
for (const [label, vp] of [['desktop', { width: 1440, height: 900 }], ['mobile', { width: 390, height: 844 }]]) {
  const ctx = await browser.newContext({ viewport: vp, deviceScaleFactor: label === 'mobile' ? 2 : 1, colorScheme: 'dark' })
  for (const [name, url] of pages) {
    if (label === 'mobile' && !mobile.includes(name)) continue
    const page = await ctx.newPage()
    const msgs = []
    page.on('console', (m) => m.type() === 'error' && msgs.push(m.text()))
    page.on('pageerror', (e) => msgs.push(String(e)))
    await page.goto(base + url, { waitUntil: 'networkidle', timeout: 60000 }).catch(() => {})
    await page.waitForTimeout(name === '09-system-check' ? 6000 : 1500)
    if (url.includes('#')) await page.evaluate((id) => document.getElementById(id)?.scrollIntoView(), url.split('#')[1])
    const file = path.join(out, `${name}${label === 'mobile' ? '-mobile' : ''}.png`)
    await page.screenshot({ path: file, fullPage: label === 'desktop' })
    // SSE connections closed by navigation are not app errors
    const real = msgs.filter((m) => !/ERR_ABORTED|ERR_CONNECTION/.test(m))
    errors += real.length
    console.log(`${real.length ? '✖' : '✔'} ${label} ${name}${real.length ? ' — ' + real.join(' | ') : ''}`)
    await page.close()
  }
  await ctx.close()
}
await browser.close()
console.log(errors ? `${errors} console error(s)` : 'no console errors')
process.exit(errors ? 1 : 0)
