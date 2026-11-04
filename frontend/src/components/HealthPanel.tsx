import { Activity, AlertTriangle, CheckCircle2, Coffee, KeyRound, Timer } from 'lucide-react'
import { useHealth } from '@/api/publish'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { cn } from '@/lib/utils'

const ago = (iso: string | null) => {
  if (!iso) return 'never'
  const m = Math.round((Date.now() - new Date(iso).getTime()) / 60000)
  if (m < 1) return 'just now'
  if (m < 60) return `${m} min ago`
  if (m < 48 * 60) return `${Math.round(m / 60)} h ago`
  return `${Math.round(m / 1440)} days ago`
}
const time = (iso: string | null) => (iso ? new Date(iso).toLocaleString('en-IN', { weekday: 'short', hour: 'numeric', minute: '2-digit' }) : '—')
const AUDIT: Record<string, string> = {
  not_applied: 'Not applied (not needed for publishing since 2 Nov 2026)', applied: 'Applied — waiting for Google',
  approved: 'Approved', rejected: 'Rejected — assisted mode still works',
}
const SOURCE: Record<string, string> = { app: 'web app', task: 'Task Scheduler', daemon: 'always-on scheduler' }

function Row({ icon: Icon, label, children, warn }: { icon: typeof Activity; label: string; children: React.ReactNode; warn?: boolean }) {
  return (
    <div className="flex items-start gap-3 text-sm">
      <Icon className={cn('mt-0.5 size-4 shrink-0', warn ? 'text-amber-300' : 'text-muted-foreground')} />
      <div className="min-w-0"><div className="text-xs text-muted-foreground">{label}</div><div className="break-words">{children}</div></div>
    </div>
  )
}

/** Health panel: is the scheduler alive, are logins OK, how much quota is left, will the laptop stay awake? */
export default function HealthPanel() {
  const { data: h } = useHealth()
  if (!h) return null
  const stale = !h.last_run_at || Date.now() - new Date(h.last_run_at).getTime() > 3 * 3600e3
  const task = h.tasks.find((t) => t.name === 'ClipForge Scheduler')
  return (
    <Card className="mb-6">
      <CardHeader><CardTitle className="flex items-center gap-2 text-base"><Activity className="size-4" /> Health</CardTitle></CardHeader>
      <CardContent className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <Row icon={Timer} label="Last scheduler run" warn={stale}>
          {ago(h.last_run_at)}{h.last_source ? ` (${SOURCE[h.last_source] ?? h.last_source})` : ''}
          {stale && <div className="text-xs text-amber-300">Not running lately — keep the app open, or run <code>clipforge schedule-task install</code>.</div>}
        </Row>
        <Row icon={Timer} label="Next run">
          {h.ticker ? `${time(h.next_run_at)} (web app, every minute)` : task ? `${task.next_run} (Task Scheduler)` : 'only when you open the app'}
          {task && <div className="text-xs text-muted-foreground">Task: {task.status}, last result {task.last_result}</div>}
        </Row>
        <Row icon={Activity} label="Mode · uploads left today">
          {h.mode === 'api' ? 'API (uploads by itself)' : 'Assisted (you upload in Studio)'} · {h.quota.inserts}/{h.quota.inserts_limit} uploads ·{' '}
          {h.quota.units.toLocaleString()} units
        </Row>
        {h.accounts.map((a) => (
          <Row key={a.id} icon={a.token.ok ? KeyRound : AlertTriangle} label={`${a.platform === 'instagram' ? 'Instagram' : 'YouTube'} · ${a.title}`} warn={!a.token.ok}>
            {a.paused ? 'Paused · ' : ''}{a.token.text}
            {a.attention > 0 && <div className="text-xs text-amber-300">{a.attention} clip(s) need attention (Schedule page)</div>}
          </Row>
        ))}
        <Row icon={CheckCircle2} label="YouTube API audit">{AUDIT[h.audit_status] ?? h.audit_status}</Row>
        <Row icon={Coffee} label="Laptop awake">
          Awake now (it answered). Keep-awake: {h.keep_awake.mode}{h.keep_awake.holding ? ` — holding: ${h.keep_awake.reason}` : ''}
        </Row>
      </CardContent>
    </Card>
  )
}
