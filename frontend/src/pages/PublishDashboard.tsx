import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { BarChart3, Download, Loader2, RefreshCw, Target, TrendingUp } from 'lucide-react'
import { toast } from 'sonner'
import { api } from '@/api/client'
import { pub, WEEKDAYS } from '@/api/publish'
import type { Dashboard, Group, YppTier } from '@/api/publishTypes'
import ChannelPicker from '@/components/ChannelPicker'
import { EmptyState, ErrorBox, PageHeader } from '@/components/common'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Progress } from '@/components/ui/progress'
import { Skeleton } from '@/components/ui/skeleton'

const fmt = (n: number) => n.toLocaleString('en-IN')
const money = (n: number) => n.toLocaleString('en-IN', { style: 'currency', currency: 'INR', maximumFractionDigits: 0 })

function Stat({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <Card className="py-4">
      <CardContent className="px-5">
        <div className="text-xs text-muted-foreground">{label}</div>
        <div className="mt-1 text-2xl font-semibold tabular-nums">{value}</div>
        {sub && <div className="text-xs text-muted-foreground">{sub}</div>}
      </CardContent>
    </Card>
  )
}

function Bar({ label, value, target, pct, eta }: { label: string; value: number; target: number; pct: number; eta: string | null }) {
  return (
    <div className="space-y-1.5">
      <div className="flex justify-between text-sm"><span>{label}</span><span className="tabular-nums text-muted-foreground">{fmt(value)} / {fmt(target)}</span></div>
      <Progress value={pct} className="h-2" />
      <div className="text-xs text-muted-foreground">
        {eta === 'reached' ? '✔ reached' : eta ? `At the current pace: ${new Date(eta).toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' })}` : 'Not growing yet — keep posting'}
      </div>
    </div>
  )
}

function Tier({ title, t, d, uploads }: { title: string; t: YppTier; d: Dashboard['ypp']; uploads?: number }) {
  const target = title.startsWith('Shorts') ? d.targets.ads : d.targets.fan
  return (
    <div className="space-y-4">
      <div className="font-medium">{title}</div>
      <Bar label="Subscribers" value={d.subscribers} target={target.subs} pct={t.subs_pct} eta={t.subs_eta} />
      <Bar label="Shorts views (90 days)" value={d.shorts_views_90d} target={target.shorts_views_90d} pct={t.views_pct} eta={t.views_eta} />
      {uploads != null && (
        <div className="text-xs text-muted-foreground">Uploads in 90 days: {d.uploads_90d}/{uploads} {d.uploads_90d >= uploads ? '✔' : ''}</div>
      )}
      <div className="text-xs text-muted-foreground">Needs ~{fmt(t.needed_views_per_day)} Shorts views/day (you: {fmt(d.shorts_views_per_day)}/day)</div>
    </div>
  )
}

function GroupList({ title, rows }: { title: string; rows: Group[] }) {
  if (!rows.length) return null
  const max = Math.max(...rows.map((r) => r.avg_views), 1)
  return (
    <div>
      <div className="mb-2 text-sm font-medium">{title}</div>
      <div className="space-y-1.5">
        {rows.map((r) => (
          <div key={r.key} className="text-xs">
            <div className="flex justify-between gap-2"><span className="truncate">{r.key}</span><span className="tabular-nums text-muted-foreground">{fmt(r.avg_views)} avg · {r.n}</span></div>
            <div className="mt-0.5 h-1.5 rounded bg-muted"><div className="h-1.5 rounded bg-primary" style={{ width: `${(r.avg_views / max) * 100}%` }} /></div>
          </div>
        ))}
      </div>
    </div>
  )
}

function Heatmap({ d }: { d: Dashboard['heatmap'] }) {
  const max = Math.max(...d.cells.map((c) => c.avg), 1)
  const hours = Array.from({ length: 24 }, (_, h) => h)
  return (
    <div className="scrollbar-thin overflow-x-auto">
      <table className="text-[10px]">
        <thead><tr><th />{hours.map((h) => <th key={h} className="w-6 font-normal text-muted-foreground">{h}</th>)}</tr></thead>
        <tbody>
          {WEEKDAYS.map((w, wi) => (
            <tr key={w}>
              <td className="pr-2 text-muted-foreground">{w}</td>
              {hours.map((h) => {
                const c = d.cells.find((x) => x.weekday === wi && x.hour === h)
                return (
                  <td key={h} title={c ? `${w} ${h}:00 — ${fmt(c.avg)} views/24h (${c.n})` : ''}>
                    <div className="m-px size-5 rounded-sm" style={{ background: c ? `oklch(0.91 0.19 103 / ${0.15 + 0.85 * (c.avg / max)})` : 'oklch(1 0 0 / 4%)' }} />
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function PublishDashboard() {
  const qc = useQueryClient()
  const [channelId, setChannelId] = useState<string>()
  const { data, isLoading, error } = useQuery({ queryKey: ['dash', channelId], queryFn: () => pub.dashboard(channelId!), enabled: !!channelId })
  const posts = useQuery({ queryKey: ['posts'], queryFn: api.posts })
  const sync = useMutation({
    mutationFn: () => pub.sync(channelId!),
    onSuccess: () => {
      toast('Syncing stats in the background…')
      setTimeout(() => qc.invalidateQueries({ queryKey: ['dash', channelId] }), 8000)
    },
    onError: (e) => toast.error(e.message),
  })
  const apply = useMutation({
    mutationFn: (slots: string[]) => pub.applySlots(channelId!, slots),
    onSuccess: () => {
      toast.success('New publish times saved')
      qc.invalidateQueries({ queryKey: ['dash', channelId] })
      qc.invalidateQueries({ queryKey: ['channels'] })
    },
  })
  const earn = (posts.data?.posts ?? []).reduce(
    (acc, p) => {
      acc.total += p.earnings
      if (p.submission_status === 'paid') acc.paid += p.earnings
      else if (p.submission_status === 'approved') acc.approved += p.earnings
      return acc
    },
    { total: 0, paid: 0, approved: 0 },
  )
  return (
    <>
      <PageHeader
        title="Dashboard"
        description={data?.last_sync_at ? `Last synced ${new Date(data.last_sync_at + (data.last_sync_at.endsWith('Z') ? '' : 'Z')).toLocaleString('en-IN')}` : 'Performance, YPP progress and earnings'}
        actions={
          <>
            <ChannelPicker value={channelId} onChange={setChannelId} />
            <Button variant="outline" onClick={() => sync.mutate()} disabled={!channelId || sync.isPending}>
              {sync.isPending ? <Loader2 className="animate-spin" /> : <RefreshCw />} Sync stats
            </Button>
            {channelId && <Button variant="ghost" asChild><a href={`/api/channels/${channelId}/stats.csv`}><Download /> CSV</a></Button>}
          </>
        }
      />
      {!channelId && !isLoading ? (
        <EmptyState icon={BarChart3} title="No channel yet" action={<Button asChild><Link to="/channels">Connect a channel</Link></Button>} />
      ) : error ? <ErrorBox error={error} /> : !data ? <Skeleton className="h-96 rounded-xl" /> : (
        <div className="space-y-6">
          <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
            <Stat label="Subscribers" value={fmt(data.ypp.subscribers)} sub={`${data.ypp.subs_per_day >= 0 ? '+' : ''}${data.ypp.subs_per_day}/day`} />
            <Stat label="Shorts views (90 d)" value={fmt(data.ypp.shorts_views_90d)} />
            <Stat label="Avg views / Short" value={fmt(data.working.avg_views)} sub={`${data.working.shorts} Shorts tracked`} />
            <Stat label="Est. campaign earnings" value={money(earn.total)} sub={`${money(earn.approved)} approved · ${money(earn.paid)} paid`} />
          </div>

          <Card>
            <CardHeader><CardTitle className="flex items-center gap-2"><Target className="size-4" /> YouTube Partner Program</CardTitle></CardHeader>
            <CardContent className="grid gap-8 md:grid-cols-2">
              <Tier title={data.ypp.targets.ads.label} t={data.ypp.ads} d={data.ypp} />
              <Tier title={data.ypp.targets.fan.label} t={data.ypp.fan} d={data.ypp} uploads={data.ypp.targets.fan.uploads_90d} />
              <p className="text-xs text-muted-foreground md:col-span-2">{data.ypp.targets.rule_note} Targets are in config.yaml (<code>ypp:</code>) — update them if YouTube changes the rules.</p>
            </CardContent>
          </Card>

          <div className="grid gap-6 lg:grid-cols-2">
            <Card>
              <CardHeader><CardTitle className="flex items-center gap-2"><TrendingUp className="size-4" /> Best publish times</CardTitle></CardHeader>
              <CardContent className="space-y-4">
                <Heatmap d={data.heatmap} />
                <div className="rounded-lg border p-3 text-sm">
                  <div>{data.recommendation.message}</div>
                  {data.recommendation.slots.length > 0 && (
                    <Button size="sm" className="mt-2" onClick={() => apply.mutate(data.recommendation.slots)} disabled={apply.isPending}>
                      Apply recommended slots ({data.recommendation.slots.join(', ')})
                    </Button>
                  )}
                  <p className="mt-2 text-xs text-muted-foreground">Rule: {data.recommendation.rule} Slots never change without your click.</p>
                </div>
              </CardContent>
            </Card>
            <Card>
              <CardHeader><CardTitle>Top Shorts</CardTitle></CardHeader>
              <CardContent>
                {!data.working.top.length ? <p className="text-sm text-muted-foreground">No published Shorts synced yet.</p> : (
                  <ol className="space-y-1.5 text-sm">
                    {data.working.top.map((t, i) => (
                      <li key={t.id} className="flex justify-between gap-3">
                        <a className="truncate hover:underline" href={`https://youtube.com/shorts/${t.id}`} target="_blank" rel="noreferrer">{i + 1}. {t.title}</a>
                        <span className="shrink-0 tabular-nums text-muted-foreground">{fmt(t.views)}</span>
                      </li>
                    ))}
                  </ol>
                )}
              </CardContent>
            </Card>
          </div>

          <Card>
            <CardHeader><CardTitle>What's working</CardTitle></CardHeader>
            <CardContent className="grid gap-6 md:grid-cols-2 lg:grid-cols-4">
              <GroupList title="Topics" rows={data.working.by_topic} />
              <GroupList title="Episodes" rows={data.working.by_episode} />
              <GroupList title="Clip length" rows={data.working.by_length} />
              <GroupList title="Title style" rows={data.working.by_title} />
              {!data.working.shorts && <p className="text-sm text-muted-foreground">Appears after your first synced Shorts.</p>}
            </CardContent>
          </Card>

          {data.reminders.length > 0 && (
            <Card>
              <CardHeader><CardTitle>Published but not submitted to a campaign</CardTitle></CardHeader>
              <CardContent className="space-y-2 text-sm">
                {data.reminders.map((r) => (
                  <div key={r.queue_id} className="flex flex-wrap items-center justify-between gap-2">
                    <a className="truncate hover:underline" href={r.youtube_url} target="_blank" rel="noreferrer">{r.title}</a>
                    <Button size="sm" variant="outline" asChild>
                      <Link to={`/tracker?queue=${r.queue_id}&title=${encodeURIComponent(r.title)}&url=${encodeURIComponent(r.youtube_url)}`}>Log submission</Link>
                    </Button>
                  </div>
                ))}
              </CardContent>
            </Card>
          )}
        </div>
      )}
    </>
  )
}
