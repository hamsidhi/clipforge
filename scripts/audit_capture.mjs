// YouTube API audit attachments: screenshots of the public pages + the upload/schedule/privacy/revoke screens,
// and a screencast of the whole flow (Playwright video → mp4 with ffmpeg). Demo data only — never real tokens.
// Usage (demo server running, see AUDIT_KIT.md): node scripts/audit_capture.mjs http://127.0.0.1:8010
import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import { createRequire } from 'node:module'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const require = createRequire(path.join(here, '../frontend/package.json'))
const { chromium } = require('playwright-core')

const base = process.argv[2] ?? 'http://127.0.0.1:8010'
const out = path.join(here, '..', 'docs', 'audit')
const site = path.join(here, '..', 'docs-site')
fs.mkdirSync(out, { recursive: true })

let browser
for (const channel of ['msedge', 'chrome']) {
  try {
    browser = await chromium.launch({ channel })
    break
  } catch { /* next */ }
}
if (!browser) throw new Error('No Edge/Chrome found')

const errors = []
const watch = (page) => {
  page.on('console', (m) => m.type() === 'error' && errors.push(m.text()))
  page.on('pageerror', (e) => errors.push(String(e)))
}
const shot = (page, name, opts = {}) => page.screenshot({ path: path.join(out, `${name}.png`), ...opts })
const settle = (page, ms = 1200) => page.waitForTimeout(ms)

// ---- public pages (GitHub Pages content, rendered from docs-site/)
{
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 }, colorScheme: 'dark' })
  const page = await ctx.newPage()
  for (const [name, file] of [['01-privacy-policy', 'privacy.html'], ['02-homepage', 'index.html'], ['03-terms', 'terms.html']]) {
    await page.goto(pathToFileURL(path.join(site, file)).href)
    await shot(page, name, { fullPage: true })
  }
  await ctx.close()
}

// ---- app screens + screencast
const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 }, colorScheme: 'dark' })
const page = await ctx.newPage()
watch(page)
// screencast via Chrome DevTools frames (no extra Playwright ffmpeg download needed)
const frames = []
const cdp = await ctx.newCDPSession(page)
cdp.on('Page.screencastFrame', async (f) => {
  frames.push({ data: f.data, t: f.metadata.timestamp })
  await cdp.send('Page.screencastFrameAck', { sessionId: f.sessionId }).catch(() => {})
})
await cdp.send('Page.startScreencast', { format: 'jpeg', quality: 80, maxWidth: 1280, maxHeight: 800, everyNthFrame: 1 })
await page.goto(base + '/channels', { waitUntil: 'networkidle' })
await settle(page)
await shot(page, '04-accounts-connect')

await page.goto(base + '/schedule', { waitUntil: 'networkidle' })
await settle(page, 1500)
await shot(page, '07-schedule')
await page.getByRole('tab', { name: /List/ }).click()
await settle(page, 600)
// open a clip that isn't on YouTube yet → the upload form
const rows = page.locator('div.divide-y > div button.min-w-0')
const n = await rows.count()
for (let i = 0; i < n; i++) {
  await rows.nth(i).click()
  await settle(page, 700)
  const editable = await page.locator('[role=dialog] input:not([disabled])').first().isVisible().catch(() => false)
  if (editable && !(await page.locator('[role=dialog] input[disabled]').first().isVisible().catch(() => false))) break
  await page.keyboard.press('Escape')
  await settle(page, 300)
}
await shot(page, '05-upload-dialog')
await page.locator('[role=dialog]').evaluate((el) => el.scrollTo(0, el.scrollHeight))
await settle(page, 500)
await shot(page, '05b-upload-dialog-terms-link')
await page.locator('[role=dialog]').evaluate((el) => el.scrollTo(0, 0))
await settle(page, 300)
await page.locator('[role=dialog] [role=combobox]').first().click()
await settle(page, 500)
await shot(page, '06-privacy-selector')
await page.keyboard.press('Escape')
await page.keyboard.press('Escape')
await settle(page, 500)

await page.goto(base + '/channels', { waitUntil: 'networkidle' })
await settle(page, 800)
const disc = page.getByRole('button', { name: /Disconnect and revoke access/ }).first()
await disc.scrollIntoViewIfNeeded()
await disc.hover()
await disc.evaluate((el) => { el.style.outline = '3px solid #ffe600'; el.style.outlineOffset = '3px' })
await settle(page, 500)
await shot(page, '08-disconnect-revoke')

await page.goto(base + '/dashboard', { waitUntil: 'networkidle' })
await settle(page, 1800)
await shot(page, '09-dashboard-health')
await page.goto(base + '/settings#audit', { waitUntil: 'networkidle' })
await settle(page, 1200)
await page.locator('#audit').scrollIntoViewIfNeeded().catch(() => {})
await settle(page, 600)
await shot(page, '10-settings-audit-checklist')

await page.waitForTimeout(1500)
await cdp.send('Page.stopScreencast').catch(() => {})
await ctx.close()
const tmp = fs.mkdtempSync(path.join(out, '.frames-'))
const lines = ['ffconcat version 1.0']
frames.forEach((f, i) => {
  const name = `f${String(i).padStart(5, '0')}.jpg`
  fs.writeFileSync(path.join(tmp, name), Buffer.from(f.data, 'base64'))
  const next = frames[i + 1]?.t ?? f.t + 1
  lines.push(`file '${name}'`, `duration ${Math.max(0.04, next - f.t).toFixed(3)}`)
})
if (frames.length) lines.push(`file 'f${String(frames.length - 1).padStart(5, '0')}.jpg'`)
fs.writeFileSync(path.join(tmp, 'list.txt'), lines.join('\n'))
const mp4 = path.join(out, 'clipforge-upload-flow.mp4')
try {
  execFileSync('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'concat', '-safe', '0', '-i', 'list.txt', '-vf',
    'scale=1280:-2,fps=25,format=yuv420p', '-c:v', 'libx264', '-crf', '28', '-movflags', '+faststart', mp4], { cwd: tmp })
  console.log(`screencast (${frames.length} frames) →`, mp4)
} finally {
  fs.rmSync(tmp, { recursive: true, force: true })
}
await browser.close()
if (errors.length) {
  console.error('Console errors:\n' + errors.join('\n'))
  process.exit(1)
}
console.log('audit captures →', out)
