import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import {
  CalendarClock, CalendarDays, ChevronLeft, ChevronRight, Download, FolderUp, Info, List, Loader2, RefreshCw, Sparkles, UploadCloud,
} from 'lucide-react'
import { toast } from 'sonner'
import { istLabel, pub, STATUS_LABEL, STATUS_STYLE, useChannels, usePublishing, useQueue, WEEKDAYS } from '@/api/publish'
import type { QueueItem, SyncResult } from '@/api/publishTypes'
import type { BulkAction } from '@/api/publish'
import FixActions from '@/components/FixActions'
import PlatformIcon from '@/components/PlatformIcon'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import ChannelPicker from '@/components/ChannelPicker'
import { EmptyState, PageHeader } from '@/components/common'
import QueueItemDialog from '@/components/QueueItemDialog'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Skeleton } from '@/components/ui/skeleton'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { cn } from '@/lib/utils'

function localDate(d: Date) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

function startOfWeek(d: Date) {
  const x = new Date(d)
  x.setHours(0, 0, 0, 0)
  x.setDate(x.getDate() - ((x.getDay() + 6) % 7)) // Monday
  return x
}

function ItemChip({ it, onOpen }: { it: QueueItem; onOpen: () => void }) {
  const draggable = ['draft', 'queued', 'needs_manual', 'failed', 'missed'].includes(it.status) && !it.youtube_video_id && !it.ig_media_id
  return (
    <button
      type="button"
      draggable={draggable}
      onDragStart={(e) => e.dataTransfer.setData('text/plain', String(it.id))}
      onClick={onOpen}
      className={cn('w-full rounded-md px-2 py-1.5 text-left text-[11px] leading-tight transition-opacity hover:opacity-80', STATUS_STYLE[it.status],
        draggable && 'cursor-grab active:cursor-grabbing')}
    >
      <div className="flex items-center gap-1 font-semibold tabular-nums"><PlatformIcon platform={it.platform} className="size-3" />{it.slot_local?.slice(11)}</div>
      <div className="line-clamp-2">{it.title || '…'}</div>
      {it.reason_label && <div className="mt-0.5 line-clamp-2 text-[10px] opacity-80">⚠ {it.reason_label}</div>}
      {it.uploading_progress != null && <div className="mt-1 h-1 rounded bg-primary" style={{ width: `${it.uploading_progress * 100}%` }} />}
    </button>
  )
}

function WeekView({ items, slots, onOpen, onDrop }: {
  items: QueueItem[]; slots: string[]; onOpen: (it: QueueItem) => void; onDrop: (id: number, slotLocal: string) => void
}) {
  const [week, setWeek] = useState(() => startOfWeek(new Date()))
  const days = Array.from({ length: 7 }, (_, i) => new Date(week.getFullYear(), week.getMonth(), week.getDate() + i))
  const byDay = useMemo(() => {
    const m: Record<string, QueueItem[]> = {}
    for (const it of items) if (it.slot_local) (m[it.slot_local.slice(0, 10)] ??= []).push(it)
    return m
  }, [items])
  const today = localDate(new Date())
  return (
    <div>
      <div className="mb-3 flex items-center gap-2">
        <Button size="icon" variant="outline" aria-label="Previous week" onClick={() => setWeek(new Date(week.getTime() - 7 * 864e5))}><ChevronLeft /></Button>
        <Button size="icon" variant="outline" aria-label="Next week" onClick={() => setWeek(new Date(week.getTime() + 7 * 864e5))}><ChevronRight /></Button>
        <Button size="sm" variant="ghost" onClick={() => setWeek(startOfWeek(new Date()))}>This week</Button>
        <span className="text-sm text-muted-foreground">{days[0].toLocaleDateString('en-IN', { day: 'numeric', month: 'short' })} – {days[6].toLocaleDateString('en-IN', { day: 'numeric', month: 'short', year: 'numeric' })} · IST</span>
      </div>
      <div className="scrollbar-thin overflow-x-auto">
        <div className="grid min-w-[840px] grid-cols-7 gap-2">
          {days.map((d, i) => {
            const key = localDate(d)
            const dayItems = (byDay[key] ?? []).sort((a, b) => (a.slot_local! < b.slot_local! ? -1 : 1))
            const past = key < today
            return (
              <div key={key} className={cn('rounded-xl border p-2', key === today && 'border-primary/50', past && 'opacity-60')}>
                <div className="mb-2 text-xs font-medium">{WEEKDAYS[i]} <span className="text-muted-foreground">{d.getDate()}</span></div>
                <div className="space-y-1.5">
                  {slots.map((s) => {
                    const at = dayItems.filter((it) => it.slot_local!.slice(11) === s)
                    return (
                      <div
                        key={s}
                        onDragOver={(e) => !past && e.preventDefault()}
                        onDrop={(e) => {
                          e.preventDefault()
                          onDrop(Number(e.dataTransfer.getData('text/plain')), `${key}T${s}`)
                        }}
                        className="min-h-12 space-y-1 rounded-lg border border-dashed border-border/60 p-1"
                      >
                        {at.length ? at.map((it) => <ItemChip key={it.id} it={it} onOpen={() => onOpen(it)} />) : (
                          <div className="px-1 py-2 text-[10px] text-muted-foreground/60">{s}</div>
                        )}
                      </div>
                    )
                  })}
                  {dayItems.filter((it) => !slots.includes(it.slot_local!.slice(11))).map((it) => (
                    <ItemChip key={it.id} it={it} onOpen={() => onOpen(it)} />
                  ))}
                </div>
              </div>
            )
          })}
        </div>
      </div>
      <p className="mt-2 text-xs text-muted-foreground">Drag a clip onto another slot to move it. Only clips not yet on YouTube can move.</p>
    </div>
  )
}

function ListView({ items, onOpen, channelId }: { items: QueueItem[]; onOpen: (it: QueueItem) => void; channelId: string }) {
  const qc = useQueryClient()
  const [sel, setSel] = useState<number[]>([])
  const bulk = useMutation({
    mutationFn: (a: { action: BulkAction; days?: number }) => pub.bulk({ ids: sel, ...a }),
    onSuccess: (r) => {
      toast.success(`${r.done.length} updated${r.errors.length ? `, ${r.errors.length} skipped: ${r.errors[0].error}` : ''}`)
      r.batches?.forEach((b) => window.open(b.url, '_blank'))
      setSel([])
      qc.invalidateQueries({ queryKey: ['queue', channelId] })
    },
    onError: (e) => toast.error(e.message),
  })
  const toggle = (id: number) => setSel((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]))
  return (
    <div className="space-y-3">
      {sel.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 rounded-lg border bg-muted/30 p-2 text-sm">
          <span className="px-2">{sel.length} selected</span>
          <Button size="sm" variant="outline" onClick={() => bulk.mutate({ action: 'shift', days: 1 })}>+1 day</Button>
          <Button size="sm" variant="outline" onClick={() => bulk.mutate({ action: 'shift', days: -1 })}>−1 day</Button>
          <Button size="sm" variant="outline" onClick={() => bulk.mutate({ action: 'unschedule' })}>Unschedule</Button>
          <Button size="sm" variant="outline" onClick={() => bulk.mutate({ action: 'regenerate' })}>Regenerate metadata</Button>
          <Button size="sm" variant="outline" onClick={() => bulk.mutate({ action: 'reschedule' })}>Next free slots</Button>
          <Button size="sm" variant="outline" onClick={() => bulk.mutate({ action: 'to_batch' })}>Switch to assisted batch</Button>
          <Button size="sm" variant="outline" onClick={() => bulk.mutate({ action: 'unbatch' })}>Upload with API instead</Button>
          <Button size="sm" variant="ghost" className="text-destructive" onClick={() => window.confirm('Remove selected clips from the queue?') && bulk.mutate({ action: 'delete' })}>Remove</Button>
        </div>
      )}
      <div className="divide-y rounded-xl border">
        {items.map((it) => (
          <div key={it.id} className="flex items-center gap-3 p-3 text-sm">
            <Checkbox checked={sel.includes(it.id)} onCheckedChange={() => toggle(it.id)} aria-label="Select" />
            <PlatformIcon platform={it.platform} className="size-4" />
            <button className="min-w-0 flex-1 text-left" onClick={() => onOpen(it)}>
              <div className="truncate font-medium">{it.title || 'Writing metadata…'}</div>
              <div className="truncate text-xs text-muted-foreground">{istLabel(it.slot_local)} · {it.episode_title} · score {it.score.toFixed(1)}{it.reason_label ? ` · ⚠ ${it.reason_label}` : it.error ? ` · ${it.error}` : ''}</div>
            </button>
            <Badge variant="outline" className={cn('shrink-0 border-transparent', STATUS_STYLE[it.status])}>{STATUS_LABEL[it.status]}</Badge>
          </div>
        ))}
      </div>
    </div>
  )
}

function SyncDialog({ result, onClose }: { result: SyncResult | null; onClose: () => void }) {
  const qc = useQueryClient()
  const link = useMutation({
    mutationFn: ({ id, vid }: { id: number; vid: string }) => pub.link(id, vid),
    onSuccess: () => {
      toast.success('Linked')
      qc.invalidateQueries({ queryKey: ['queue'] })
    },
    onError: (e) => toast.error(e.message),
  })
  return (
    <Dialog open={!!result} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader><DialogTitle>Sync with YouTube</DialogTitle></DialogHeader>
        {result && (
          <div className="space-y-3 text-sm">
            <p>Linked <b>{result.matched.length}</b> upload(s) to your queue{result.matched.some((m) => m.by === 'file name') ? ' (recognised by file name)' : ''}.</p>
            {result.unmatched_items.length > 0 && (
              <div>
                <div className="font-medium">Queue clips not found on YouTube yet</div>
                <ul className="mt-1 list-disc pl-5 text-muted-foreground">{result.unmatched_items.map((u) => <li key={u.queue_id}>{u.title}</li>)}</ul>
                <p className="mt-1 text-xs text-muted-foreground">Not uploaded yet, or uploaded from a renamed file? Link it below or from the clip.</p>
              </div>
            )}
            {result.unmatched_videos.length > 0 && (
              <div>
                <div className="font-medium">Recent Shorts not linked to the queue</div>
                <ul className="mt-1 space-y-2">
                  {result.unmatched_videos.slice(0, 8).map((v) => (
                    <li key={v.video_id} className="rounded-md border p-2">
                      <div>{v.title} <code className="text-xs text-muted-foreground">{v.file_name || v.video_id}</code></div>
                      {v.suggestions.length > 0 && (
                        <div className="mt-1 flex flex-wrap gap-1.5">
                          {v.suggestions.map((s) => (
                            <Button key={s.queue_id} size="sm" variant="outline" disabled={link.isPending}
                              onClick={() => link.mutate({ id: s.queue_id, vid: v.video_id })}>Link to “{s.title}”</Button>
                          ))}
                        </div>
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}

export default function Schedule() {
  const qc = useQueryClient()
  const [channelId, setChannelId] = useState<string>()
  const { data: channels = [], isLoading: chLoading } = useChannels()
  const { data: status } = usePublishing()
  const { data: items = [], isLoading } = useQueue(channelId)
  const [open, setOpen] = useState<QueueItem | null>(null)
  const [sync, setSync] = useState<SyncResult | null>(null)
  const [platform, setPlatform] = useState<'all' | 'youtube' | 'instagram'>('all')
  const all = channelId === 'all'
  const ch = channels.find((c) => c.id === channelId)
  const isIg = ch?.platform === 'instagram'
  const igManual = isIg && (!!ch?.extra.manual)
  const shown = items.filter((i) => platform === 'all' || i.platform === platform)
  const drafts = shown.filter((i) => i.status === 'draft')
  const planned = shown.filter((i) => i.status !== 'draft')
  const allSlots = [...new Set((all ? channels : ch ? [ch] : []).flatMap((c) => c.settings.slots))].sort()
  const laptopDue = planned.filter((i) => i.platform === 'instagram' && ['queued', 'uploading', 'ready'].includes(i.status)
    && !channels.find((c) => c.id === i.channel_id)?.extra.manual)
  const attention = shown.filter((i) => ['failed', 'missed', 'needs_manual', 'locked_private'].includes(i.status))
  const fix = useMutation({
    mutationFn: (action: BulkAction) => pub.bulk({ action, channel_id: all ? undefined : channelId }),
    onSuccess: (r) => {
      toast.success(`${r.done.length} clip(s) updated${r.errors.length ? ` — ${r.errors.length} couldn't: ${r.errors[0].error}` : ''}`)
      qc.invalidateQueries({ queryKey: ['queue', channelId] })
    },
    onError: (e) => toast.error(e.message),
  })
  const refresh = () => qc.invalidateQueries({ queryKey: ['queue', channelId] })

  const auto = useMutation({
    mutationFn: async () => {
      if (!all) return pub.autoschedule(channelId!)
      // all accounts: YouTube first, so "Instagram X hours after YouTube" can see the YouTube times
      const order = [...channels].sort((a, b) => (a.platform === 'instagram' ? 1 : 0) - (b.platform === 'instagram' ? 1 : 0))
      const res = { scheduled: [] as QueueItem[], still_draft: [] as { id: number; title: string; meta_status: string; problems: string[] }[] }
      for (const c of order.filter((c) => !c.paused)) {
        const r = await pub.autoschedule(c.id)
        res.scheduled.push(...r.scheduled)
        res.still_draft.push(...r.still_draft)
      }
      return res
    },
    onSuccess: (r) => {
      toast.success(`${r.scheduled.length} clip(s) scheduled`, {
        description: r.still_draft.length ? `${r.still_draft.length} still need attention (open them in Drafts).` : undefined,
      })
      refresh()
    },
    onError: (e) => toast.error(e.message),
  })
  const batch = useMutation({
    mutationFn: () => pub.prepareBatch(channelId!),
    onSuccess: (b) => {
      toast.success(`Batch ready: ${b.count} clips`, { description: b.folder })
      window.open(b.url, '_blank')
      refresh()
    },
    onError: (e) => toast.error(e.message),
  })
  const doSync = useMutation({
    mutationFn: () => pub.sync(channelId!),
    onSuccess: (r) => {
      setSync(r)
      refresh()
    },
    onError: (e) => toast.error(e.message),
  })
  const run = useMutation({
    mutationFn: pub.runScheduler,
    onSuccess: () => toast('Uploading due clips in the background…'),
  })
  const move = useMutation({
    mutationFn: ({ id, slot }: { id: number; slot: string }) => pub.patch(id, { slot_local: slot }),
    onSuccess: () => {
      toast.success('Moved')
      refresh()
    },
    onError: (e) => toast.error(e.message),
  })

  if (!chLoading && !channels.length) {
    return (
      <>
        <PageHeader title="Schedule" />
        <EmptyState icon={CalendarClock} title="Connect a channel first" action={<Button asChild><Link to="/channels">Go to Channels</Link></Button>}>
          ClipForge plans your Shorts per YouTube channel.
        </EmptyState>
      </>
    )
  }
  const mode = status?.mode ?? 'assisted'
  return (
    <>
      <PageHeader
        title="Schedule"
        description={
          <span className="flex flex-wrap items-center gap-2">
            {!isIg && <Badge variant="outline">{mode === 'api' ? 'YouTube: API mode — ClipForge uploads' : 'YouTube: assisted — you upload in Studio'}</Badge>}
            {isIg && <Badge variant="outline">{igManual ? 'Instagram: assisted — you post' : 'Instagram: ClipForge posts at each slot'}</Badge>}
            {status && <span>{status.uploads_left_today}/{status.quota.inserts_limit} API uploads left today</span>}
            {ch?.paused && <Badge variant="outline" className="text-amber-300">channel paused</Badge>}
          </span>
        }
        actions={
          <>
            <ChannelPicker value={channelId} onChange={setChannelId} allowAll />
            <Button onClick={() => auto.mutate()} disabled={!channelId || auto.isPending}>
              {auto.isPending ? <Loader2 className="animate-spin" /> : <Sparkles />} Schedule approved clips
            </Button>
            {!all && (isIg ? igManual : mode === 'assisted') ? (
              <Button variant="outline" onClick={() => batch.mutate()} disabled={!channelId || batch.isPending}>
                <FolderUp /> {isIg ? 'Prepare Instagram kit' : 'Prepare batch'}
              </Button>
            ) : (
              <Button variant="outline" onClick={() => run.mutate()} disabled={run.isPending}><UploadCloud /> Run scheduler now</Button>
            )}
            {!all && !isIg && (
              <Button variant="outline" onClick={() => doSync.mutate()} disabled={!channelId || doSync.isPending}>
                {doSync.isPending ? <Loader2 className="animate-spin" /> : <RefreshCw />} Sync with YouTube
              </Button>
            )}
          </>
        }
      />
      {status?.banner && (
        <div className={cn('mb-5 flex items-start gap-3 rounded-xl border p-4 text-sm',
          status.banner.level === 'warning' ? 'border-orange-500/40 bg-orange-500/10' : 'border-sky-500/30 bg-sky-500/10')}>
          <Info className="mt-0.5 size-4 shrink-0" />
          <div>{status.banner.text} {status.banner.link && <a className="text-primary underline" href={status.banner.link} target="_blank" rel="noreferrer">Audit &amp; quota form</a>}</div>
        </div>
      )}
      {laptopDue.length > 0 && (
        <div className="mb-5 flex items-start gap-3 rounded-xl border border-pink-500/30 bg-pink-500/10 p-4 text-sm">
          <PlatformIcon platform="instagram" className="mt-0.5 size-4" />
          <div>
            <b>Laptop must be on</b> at {laptopDue.slice(0, 3).map((i) => istLabel(i.slot_local)).join(', ')}{laptopDue.length > 3 ? ` and ${laptopDue.length - 3} more` : ''} with
            ClipForge running: Instagram has no scheduling, so ClipForge publishes each Reel itself at its slot.
          </div>
        </div>
      )}
      {ch && !ch.confirmed && (mode === 'api' || isIg) && (
        <div className="mb-5 rounded-xl border border-amber-500/30 bg-amber-500/10 p-4 text-sm">
          Posting to <b>{ch.title}</b> is blocked until you confirm the account on the <Link className="text-primary underline" to="/channels">Accounts</Link> page.
        </div>
      )}

      {attention.length > 0 && (
        <Card className="mb-6 border-amber-500/30">
          <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-2">
            <CardTitle className="text-base">Needs attention — {attention.length} clip(s)</CardTitle>
            <div className="flex flex-wrap gap-2">
              {attention.some((i) => ['missed', 'needs_manual'].includes(i.status) && !i.youtube_video_id) && (
                <Button size="sm" onClick={() => fix.mutate('reschedule_missed')} disabled={fix.isPending}>Reschedule all missed to next free slots</Button>
              )}
              {attention.some((i) => i.status === 'failed') && (
                <Button size="sm" variant="outline" onClick={() => fix.mutate('retry_failed')} disabled={fix.isPending}>Retry all failed</Button>
              )}
            </div>
          </CardHeader>
          <CardContent className="grid gap-3 lg:grid-cols-2">
            {attention.map((it) => (
              <div key={it.id} className="space-y-2">
                <button className="block w-full truncate text-left text-sm font-medium hover:text-primary" onClick={() => setOpen(it)}>
                  {istLabel(it.slot_local)} · {it.title}
                </button>
                <FixActions item={it} onLink={() => setOpen(it)} compact />
              </div>
            ))}
          </CardContent>
        </Card>
      )}

      {drafts.length > 0 && (
        <Card className="mb-6">
          <CardHeader><CardTitle className="text-base">Drafts — {drafts.length} clip(s) not scheduled yet</CardTitle></CardHeader>
          <CardContent className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {drafts.map((d) => (
              <button key={d.id} onClick={() => setOpen(d)} className="rounded-lg border p-3 text-left text-sm hover:border-primary/50">
                <div className="line-clamp-2 font-medium">{d.meta_status === 'pending' ? <span className="flex items-center gap-2 text-muted-foreground"><Loader2 className="size-3 animate-spin" />Writing metadata…</span> : d.title}</div>
                <div className="mt-1 text-xs text-muted-foreground">{d.episode_title} · score {d.score.toFixed(1)}</div>
                {d.problems.length > 0 && <div className="mt-1 text-xs text-amber-300">{d.problems[0]}</div>}
              </button>
            ))}
          </CardContent>
        </Card>
      )}

      {isLoading || !channelId ? <Skeleton className="h-96 rounded-xl" /> : !planned.length ? (
        <EmptyState icon={CalendarDays} title="Nothing scheduled yet">
          Approve clips in a job's gallery, use <b>Add to schedule</b> there, then click <b>Schedule approved clips</b>.
        </EmptyState>
      ) : (
        <Tabs defaultValue="week">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <TabsList>
              <TabsTrigger value="week"><CalendarDays /> Week</TabsTrigger>
              <TabsTrigger value="list"><List /> List</TabsTrigger>
            </TabsList>
            <div className="flex items-center gap-2">
              {all && (
                <ToggleGroup type="single" size="sm" variant="outline" value={platform} onValueChange={(v) => v && setPlatform(v as typeof platform)}>
                  <ToggleGroupItem value="all">All</ToggleGroupItem>
                  <ToggleGroupItem value="youtube"><PlatformIcon platform="youtube" /> YouTube</ToggleGroupItem>
                  <ToggleGroupItem value="instagram"><PlatformIcon platform="instagram" /> Instagram</ToggleGroupItem>
                </ToggleGroup>
              )}
              <Button variant="ghost" size="sm" asChild><a href={`/api/queue.csv${all ? '' : `?channel_id=${channelId}`}`}><Download /> CSV</a></Button>
            </div>
          </div>
          <TabsContent value="week">
            <WeekView items={planned} slots={allSlots} onOpen={setOpen} onDrop={(id, slot) => move.mutate({ id, slot })} />
          </TabsContent>
          <TabsContent value="list"><ListView items={planned} onOpen={setOpen} channelId={channelId!} /></TabsContent>
        </Tabs>
      )}
      <div className="mt-6 flex flex-wrap gap-3 text-xs">
        {Object.entries(STATUS_LABEL).map(([k, v]) => <span key={k} className={cn('rounded px-2 py-0.5', STATUS_STYLE[k])}>{v}</span>)}
      </div>
      <QueueItemDialog item={open ? items.find((i) => i.id === open.id) ?? open : null} onClose={() => setOpen(null)} />
      <SyncDialog result={sync} onClose={() => setSync(null)} />
    </>
  )
}
