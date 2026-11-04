import { useQuery } from '@tanstack/react-query'
import { Server } from 'lucide-react'
import { request } from '@/api/client'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'

interface WorkerState {
  connected: boolean; server: string; last_contact: number | null; last_heartbeat: number | null
  task: { id: number; job_id: number; stage: string; title?: string } | null; progress: number; message: string; error: string; done: number
}
interface Node { mode: 'standalone' | 'server' | 'worker'; node: string; server_url: string; worker: WorkerState | null }
interface W { id: string; name: string; online: boolean; capabilities: string[]; last_seen: string | null; task: { job_id: number; stage: string; progress: number } | null }

const ago = (t: number | null) => (t ? `${Math.max(0, Math.round(Date.now() / 1000 - t))} s ago` : 'never')

export default function NodeStatus() {
  const node = useQuery({ queryKey: ['node'], queryFn: () => request<Node>('/api/node/status'), refetchInterval: 10000 })
  const workers = useQuery({
    queryKey: ['workers'], queryFn: () => request<W[]>('/api/server/workers'), refetchInterval: 15000,
    enabled: node.data?.mode === 'server',
  })
  const n = node.data
  if (!n) return null
  const w = n.worker
  return (
    <Card id="node">
      <CardHeader>
        <CardTitle className="flex items-center gap-2"><Server className="size-5" /> This computer: {n.node} ({n.mode})</CardTitle>
        <CardDescription>
          {n.mode === 'standalone' && 'Everything runs here (default). An always-on server is optional — see AUTOMATION_GUIDE Part I.'}
          {n.mode === 'server' && 'Always-on server: database, logins, publishing and the Telegram bot live here; laptops take heavy jobs.'}
          {n.mode === 'worker' && `Worker of ${n.server_url}: takes heavy jobs, never publishes, never runs the Telegram bot.`}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-2 text-sm">
        {w && (
          <div className="grid gap-1 sm:grid-cols-2">
            <div>Server: <b>{w.connected ? 'connected' : 'not reachable'}</b></div>
            <div>Last contact: {ago(w.last_contact)} · heartbeat {ago(w.last_heartbeat)}</div>
            <div className="sm:col-span-2">
              {w.task ? <>Working on job {w.task.job_id} — {w.task.stage} {Math.round(w.progress * 100)}% {w.message && `· ${w.message}`}</> : 'Idle — waiting for work.'}
            </div>
            <div>Tasks done since start: {w.done}</div>
            {w.error && <div className="text-amber-600 sm:col-span-2">Last problem: {w.error}</div>}
          </div>
        )}
        {n.mode === 'server' && (
          <div className="space-y-1">
            {(workers.data ?? []).length === 0 && <div className="text-muted-foreground">No workers yet: <code>clipforge server add-worker laptop</code></div>}
            {(workers.data ?? []).map((x) => (
              <div key={x.id} className="flex flex-wrap justify-between gap-2 rounded-md border px-3 py-2">
                <span>{x.online ? '🟢' : '⚪'} <b>{x.name}</b> · {x.capabilities.join(', ')}</span>
                <span className="text-muted-foreground">{x.task ? `job ${x.task.job_id} ${x.task.stage} ${Math.round(x.task.progress * 100)}%` : x.online ? 'idle' : 'offline'}</span>
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  )
}
