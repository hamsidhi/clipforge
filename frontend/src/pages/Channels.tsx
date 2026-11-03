import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, ChevronDown, KeyRound, Loader2, Plus, RefreshCw, ShieldCheck, Trash2, Tv } from 'lucide-react'
import { toast } from 'sonner'
import { pub, useChannels, usePublishing, WEEKDAYS } from '@/api/publish'
import type { Channel, ChannelSettings } from '@/api/publishTypes'
import { ChannelAvatar } from '@/components/ChannelPicker'
import { EmptyState, ErrorBox, PageHeader } from '@/components/common'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Switch } from '@/components/ui/switch'
import { Textarea } from '@/components/ui/textarea'
import { cn } from '@/lib/utils'

async function connect() {
  try {
    const { url } = await pub.oauthStart()
    window.location.href = url
  } catch (e) {
    toast.error((e as Error).message)
  }
}

function ConfirmDialog({ ch, open, onOpenChange }: { ch: Channel; open: boolean; onOpenChange: (o: boolean) => void }) {
  const qc = useQueryClient()
  const m = useMutation({
    mutationFn: () => pub.confirmChannel(ch.id),
    onSuccess: () => {
      toast.success(`Uploads to ${ch.title} confirmed`)
      qc.invalidateQueries({ queryKey: ['channels'] })
      onOpenChange(false)
    },
  })
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Upload Shorts to this channel?</DialogTitle>
          <DialogDescription>Check it's the right one — not your music or personal channel.</DialogDescription>
        </DialogHeader>
        <div className="flex items-center gap-4 rounded-xl border p-4">
          <ChannelAvatar ch={ch} className="size-14 text-xl" />
          <div>
            <div className="text-lg font-semibold">{ch.title}</div>
            <div className="text-sm text-muted-foreground">{ch.handle || ch.id}</div>
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button onClick={() => m.mutate()} disabled={m.isPending}><ShieldCheck /> Yes, this is the right channel</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1.5">
      <Label>{label}</Label>
      {children}
      {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
    </div>
  )
}

function SettingsForm({ ch }: { ch: Channel }) {
  const qc = useQueryClient()
  const [s, setS] = useState<ChannelSettings>(ch.settings)
  const [slotsText, setSlotsText] = useState(ch.settings.slots.join(', '))
  const [peakText, setPeakText] = useState(ch.settings.peak_slots.join(', '))
  useEffect(() => setS(ch.settings), [ch.settings])
  const save = useMutation({
    mutationFn: () => {
      const split = (t: string) => t.split(/[,\s]+/).filter(Boolean)
      return pub.patchChannel(ch.id, { settings: { ...s, slots: split(slotsText), peak_slots: split(peakText) } })
    },
    onSuccess: () => {
      toast.success('Channel settings saved')
      qc.invalidateQueries({ queryKey: ['channels'] })
    },
    onError: (e) => toast.error(e.message),
  })
  const set = <K extends keyof ChannelSettings>(k: K, v: ChannelSettings[K]) => setS((p) => ({ ...p, [k]: v }))
  return (
    <div className="space-y-6 border-t p-5">
      <div className="grid gap-5 md:grid-cols-3">
        <Field label="Publish times (IST)" hint="Comma-separated, e.g. 12:30, 18:30, 21:00">
          <Input value={slotsText} onChange={(e) => setSlotsText(e.target.value)} />
        </Field>
        <Field label="Peak times" hint="Your strongest clips go here">
          <Input value={peakText} onChange={(e) => setPeakText(e.target.value)} />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Posts / day">
            <Input type="number" min={1} max={20} value={s.posts_per_day} onChange={(e) => set('posts_per_day', Number(e.target.value))} />
          </Field>
          <Field label="Min gap (h)">
            <Input type="number" min={0} step={0.5} value={s.min_gap_hours} onChange={(e) => set('min_gap_hours', Number(e.target.value))} />
          </Field>
        </div>
      </div>
      <div className="grid gap-5 md:grid-cols-3">
        <Field label="Posting days">
          <div className="flex flex-wrap gap-1">
            {WEEKDAYS.map((d, i) => (
              <button
                key={d}
                type="button"
                onClick={() => set('days', s.days.includes(i) ? s.days.filter((x) => x !== i) : [...s.days, i].sort())}
                className={cn('rounded-md border px-2 py-1 text-xs', s.days.includes(i) ? 'border-primary bg-primary/15 text-primary' : 'text-muted-foreground')}
              >
                {d}
              </button>
            ))}
          </div>
        </Field>
        <Field label="Start date" hint="Empty = today">
          <Input type="date" value={s.start_date ?? ''} onChange={(e) => set('start_date', e.target.value || null)} />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Title language">
            <Select value={s.title_language} onValueChange={(v) => set('title_language', v as ChannelSettings['title_language'])}>
              <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="hinglish">Hinglish</SelectItem>
                <SelectItem value="english">English</SelectItem>
              </SelectContent>
            </Select>
          </Field>
          <Field label="Category">
            <Select value={s.category_id} onValueChange={(v) => set('category_id', v)}>
              <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="27">Education</SelectItem>
                <SelectItem value="22">People &amp; Blogs</SelectItem>
                <SelectItem value="24">Entertainment</SelectItem>
                <SelectItem value="25">News &amp; Politics</SelectItem>
              </SelectContent>
            </Select>
          </Field>
        </div>
      </div>
      <div className="grid gap-5 md:grid-cols-2">
        <Field label="Description template" hint="Added after the hook summary and credit line">
          <Textarea rows={3} value={s.description_template} onChange={(e) => set('description_template', e.target.value)} />
        </Field>
        <Field label="Credit line" hint="{episode}, {source_channel} and {url} are filled in for each clip">
          <Textarea rows={3} value={s.credit_template} onChange={(e) => set('credit_template', e.target.value)} />
          <div className="flex gap-2 rounded-lg border border-amber-500/30 bg-amber-500/10 p-2.5 text-xs text-amber-200">
            <AlertTriangle className="size-4 shrink-0" />
            Clips of other creators should only be posted under an official clipping campaign or with their permission.
            Keep the credit line.
          </div>
        </Field>
      </div>
      <div className="grid gap-5 md:grid-cols-4">
        <Field label="Niche hashtag"><Input value={s.niche_tag} onChange={(e) => set('niche_tag', e.target.value)} /></Field>
        <Field label="Campaign (Whop)"><Input value={s.whop_campaign} onChange={(e) => set('whop_campaign', e.target.value)} /></Field>
        <Field label="Rate per 1k views (₹)">
          <Input type="number" min={0} value={s.campaign_rate_per_1k} onChange={(e) => set('campaign_rate_per_1k', Number(e.target.value))} />
        </Field>
        <Field label="Campaign fee %">
          <Input type="number" min={0} max={100} value={s.campaign_fee_pct} onChange={(e) => set('campaign_fee_pct', Number(e.target.value))} />
        </Field>
      </div>
      <Button onClick={() => save.mutate()} disabled={save.isPending}>Save settings</Button>
    </div>
  )
}

function ChannelCard({ ch }: { ch: Channel }) {
  const qc = useQueryClient()
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [open, setOpen] = useState(false)
  const pause = useMutation({
    mutationFn: (paused: boolean) => pub.patchChannel(ch.id, { paused }),
    onSuccess: (c) => {
      toast(c.paused ? 'Channel paused — nothing will be uploaded' : 'Channel active again')
      qc.invalidateQueries({ queryKey: ['channels'] })
    },
  })
  const check = useMutation({
    mutationFn: () => pub.checkAuth(ch.id),
    onSuccess: (r) => {
      toast[r.ok ? 'success' : 'error'](r.ok ? 'Login works' : 'Login expired — reconnect')
      qc.invalidateQueries({ queryKey: ['channels'] })
    },
    onError: (e) => toast.error(e.message),
  })
  const del = useMutation({
    mutationFn: () => pub.deleteChannel(ch.id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['channels'] }),
    onError: (e) => toast.error(e.message),
  })
  return (
    <Card className="gap-0 py-0">
      <CardContent className="flex flex-col gap-4 p-5 sm:flex-row sm:items-center">
        <div className="flex min-w-0 flex-1 items-center gap-4">
        <ChannelAvatar ch={ch} className="size-12 shrink-0 text-lg" />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2 font-medium">
            {ch.title}
            {ch.auth_status === 'ok' ? (
              <Badge variant="outline" className="border-transparent bg-success/15 text-success">connected</Badge>
            ) : (
              <Badge variant="outline" className="border-transparent bg-destructive/15 text-destructive">needs re-login</Badge>
            )}
            {ch.paused && <Badge variant="outline">paused</Badge>}
            {!ch.confirmed && <Badge variant="outline" className="text-amber-300">not confirmed for uploads</Badge>}
          </div>
          <div className="text-xs text-muted-foreground">
            {ch.handle || ch.id} · {Object.entries(ch.counts).map(([k, v]) => `${v} ${k}`).join(' · ') || 'nothing queued'}
          </div>
          {ch.auth_status !== 'ok' && <div className="mt-1 text-xs text-destructive">{ch.auth_error}</div>}
        </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {ch.auth_status !== 'ok' && <Button size="sm" onClick={connect}><RefreshCw /> Reconnect</Button>}
          {!ch.confirmed && <Button size="sm" variant="outline" onClick={() => setConfirmOpen(true)}><ShieldCheck /> Confirm</Button>}
          <Button size="sm" variant="ghost" onClick={() => check.mutate()} disabled={check.isPending}>Test login</Button>
          <div className="flex items-center gap-2 px-2 text-sm">
            <Switch checked={!ch.paused} onCheckedChange={(v) => pause.mutate(!v)} id={`p-${ch.id}`} />
            <Label htmlFor={`p-${ch.id}`}>{ch.paused ? 'Paused' : 'Active'}</Label>
          </div>
          <Button size="icon" variant="ghost" aria-label="Remove channel"
            onClick={() => window.confirm(`Remove ${ch.title}? Its login is deleted; scheduled YouTube videos stay on YouTube.`) && del.mutate()}>
            <Trash2 />
          </Button>
        </div>
      </CardContent>
      <Collapsible open={open} onOpenChange={setOpen}>
        <CollapsibleTrigger asChild>
          <button className="flex w-full items-center justify-between border-t px-5 py-3 text-sm text-muted-foreground hover:text-foreground">
            Schedule &amp; metadata settings — {ch.settings.slots.join(', ')} IST · {ch.settings.posts_per_day}/day
            <ChevronDown className={cn('size-4 transition-transform', open && 'rotate-180')} />
          </button>
        </CollapsibleTrigger>
        <CollapsibleContent><SettingsForm ch={ch} /></CollapsibleContent>
      </Collapsible>
      <ConfirmDialog ch={ch} open={confirmOpen} onOpenChange={setConfirmOpen} />
    </Card>
  )
}

function PickDialog({ pid, onDone }: { pid: string; onDone: () => void }) {
  const qc = useQueryClient()
  const { data: options } = useQuery({ queryKey: ['pick', pid], queryFn: () => pub.pickOptions(pid) })
  const pick = useMutation({
    mutationFn: (id: string) => pub.pick(pid, id),
    onSuccess: (c) => {
      toast.success(`Connected ${c.title}`)
      qc.invalidateQueries({ queryKey: ['channels'] })
      onDone()
    },
    onError: (e) => toast.error(e.message),
  })
  return (
    <Dialog open onOpenChange={(o) => !o && onDone()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Which channel?</DialogTitle>
          <DialogDescription>This Google login manages several channels. Pick the one to connect.</DialogDescription>
        </DialogHeader>
        <div className="space-y-2">
          {(options ?? []).map((o) => (
            <button key={o.id} onClick={() => pick.mutate(o.id)}
              className="flex w-full items-center gap-3 rounded-lg border p-3 text-left hover:border-primary/50">
              <ChannelAvatar ch={o} />
              <div><div className="font-medium">{o.title}</div><div className="text-xs text-muted-foreground">{o.handle || o.id}</div></div>
            </button>
          ))}
        </div>
      </DialogContent>
    </Dialog>
  )
}

export default function Channels() {
  const { data, isLoading, error } = useChannels()
  const { data: status } = usePublishing()
  const [params, setParams] = useSearchParams()
  const pid = params.get('pick')
  useEffect(() => {
    if (params.get('connected')) {
      toast.success('Channel connected — confirm it before the first upload')
      setParams({}, { replace: true })
    }
    if (params.get('error')) {
      toast.error(`Google sign-in failed: ${params.get('error')}`)
      setParams({}, { replace: true })
    }
  }, [params, setParams])
  const noSecret = status && !status.client_secret
  return (
    <>
      <PageHeader
        title="Channels"
        description="YouTube channels ClipForge can schedule Shorts for. Logins stay on this computer."
        actions={<Button onClick={connect} disabled={!!noSecret}><Plus /> Add channel</Button>}
      />
      {noSecret && (
        <div className="mb-5 flex items-start gap-3 rounded-xl border border-amber-500/30 bg-amber-500/10 p-4 text-sm">
          <KeyRound className="mt-0.5 size-4 shrink-0 text-amber-300" />
          <div>
            First add your Google OAuth client file (free, ~10 minutes, one time).{' '}
            <Link to="/settings#publishing" className="font-medium text-primary underline">Settings → Publishing</Link> has the steps.
          </div>
        </div>
      )}
      {status && (
        <Collapsible className="mb-5">
          <CollapsibleTrigger className="text-xs text-muted-foreground underline">What does ClipForge ask Google for?</CollapsibleTrigger>
          <CollapsibleContent className="mt-2 space-y-1 text-xs text-muted-foreground">
            {status.scope_help.map((s) => (
              <div key={s.scope}><code className="text-foreground">{s.scope.split('/').pop()}</code> — {s.why}</div>
            ))}
            <div>Add channels from this laptop (http://localhost:8000): Google sends the login back to localhost.</div>
          </CollapsibleContent>
        </Collapsible>
      )}
      {error ? <ErrorBox error={error} /> : isLoading ? <Skeleton className="h-40 rounded-xl" /> : !data?.length ? (
        <EmptyState icon={Tv} title="No channels yet" action={<Button onClick={connect} disabled={!!noSecret}><Plus /> Add channel</Button>}>
          Connect the YouTube channel(s) you post Shorts to. You can add several (Brand Accounts too).
        </EmptyState>
      ) : (
        <div className="space-y-4">{data.map((c) => <ChannelCard key={c.id} ch={c} />)}</div>
      )}
      {pid && <PickDialog pid={pid} onDone={() => setParams({}, { replace: true })} />}
      {status?.worker.current && (
        <div className="mt-4 flex items-center gap-2 text-xs text-muted-foreground"><Loader2 className="size-3 animate-spin" /> {status.worker.current}</div>
      )}
    </>
  )
}
