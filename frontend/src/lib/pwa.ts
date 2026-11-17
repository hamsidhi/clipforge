import { useEffect, useState } from 'react'

interface BeforeInstallPromptEvent extends Event {
  prompt: () => Promise<void>
  userChoice: Promise<{ outcome: 'accepted' | 'dismissed' }>
}

let deferred: BeforeInstallPromptEvent | null = null
const listeners = new Set<() => void>()

if (typeof window !== 'undefined') {
  window.addEventListener('beforeinstallprompt', (e) => {
    e.preventDefault() // show our own "Install app" button instead of the mini-infobar
    deferred = e as BeforeInstallPromptEvent
    listeners.forEach((l) => l())
  })
  window.addEventListener('appinstalled', () => {
    deferred = null
    listeners.forEach((l) => l())
  })
}

export function useInstallPrompt() {
  const [, force] = useState(0)
  useEffect(() => {
    const l = () => force((n) => n + 1)
    listeners.add(l)
    return () => {
      listeners.delete(l)
    }
  }, [])
  return {
    canInstall: !!deferred,
    install: async () => {
      if (!deferred) return
      await deferred.prompt()
      await deferred.userChoice
      deferred = null
      listeners.forEach((l) => l())
    },
  }
}

/** Service worker: app shell only (the API is never cached). Needs HTTPS or localhost. */
export function registerServiceWorker() {
  if (!('serviceWorker' in navigator)) return
  if (location.protocol !== 'https:' && !['localhost', '127.0.0.1'].includes(location.hostname)) return
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(() => {
      /* not critical */
    })
  })
}

/** The phone app stays open for days: when the laptop gets a new build, reload into it (on open/focus + every 5 min). */
export function watchForUpdates() {
  const current = (document.querySelector('script[type="module"][src*="/assets/index-"]') as HTMLScriptElement | null)?.src
  if (!current) return  // dev server
  const mine = new URL(current).pathname
  let busy = false
  const check = async () => {
    if (busy || document.visibilityState !== 'visible') return
    busy = true
    try {
      const html = await (await fetch('/', { cache: 'no-store', credentials: 'same-origin' })).text()
      const latest = html.match(/\/assets\/index-[\w-]+\.js/)?.[0]
      if (latest && latest !== mine) {
        try {
          if (sessionStorage.getItem('cf-reloaded-for') === latest) return  // already tried once: no reload loop
          sessionStorage.setItem('cf-reloaded-for', latest)
        } catch { /* private mode */ }
        const reg = await navigator.serviceWorker?.getRegistration?.()
        await reg?.update().catch(() => {})
        location.reload()
      }
    } catch {
      /* laptop unreachable: try later */
    } finally {
      busy = false
    }
  }
  document.addEventListener('visibilitychange', check)
  window.addEventListener('focus', check)
  window.addEventListener('pageshow', check)
  setInterval(check, 5 * 60_000)
  setTimeout(check, 3000)
}
