import { useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Camera, ChevronDown, KeyRound, Laptop, Loader2, Plus, RefreshCw, ShieldCheck, Tv, Unplug, UserPlus } from 'lucide-react'
import { toast } from 'sonner'
import { pub, useChannels, useMetaApp, usePublishing, WEEKDAYS } from '@/api/publish'
import type { Channel, ChannelSettings } from '@/api/publishTypes'
import PlatformIcon from '@/components/PlatformIcon'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
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

async function connectInstagram() {
  try {
    const { url } = await pub.igOauthStart()
    window.location.href = url
  } catch (e) {
    toast.error((e as Error).message)
  }
}

const isIg = (ch: Pick<Channel, 'platform'>) => ch.platform === 'instagram'

function ConfirmDialog({ ch, open, onOpenChange }: { ch: Channel; open: boolean; onOpenChange: (o: boolean) => void }) {
  const qc = useQueryClient()
  const m = useMutation({
    mutationFn: () => pub.confirmChannel(ch.id),
    onSuccess: () => {
      toast.success(isIg(ch) ? `Posting to @${ch.title} confirmed` : `Uploads to ${ch.title} confirmed`)
      qc.invalidateQueries({ queryKey: ['channels'] })
      onOpenChange(false)
    },
  })
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{isIg(ch) ? 'Post Reels to this Instagram account?' : 'Upload Shorts to this channel?'}</DialogTitle>
          <DialogDescription>Check it's the right one — not your personal or another brand's account.</DialogDescription>
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
          <Button onClick={() => m.mutate()} disabled={m.isPending}><ShieldCheck /> Yes, this is the right {isIg(ch) ? 'account' : 'channel'}</Button>
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
      {isIg(ch) && (
        <div className="grid gap-5 md:grid-cols-3">
          <Field label="Hours after YouTube" hint="When a clip also goes to YouTube, post the Reel at least this many hours later (0 = any later slot)">
            <Input type="number" min={0} step={0.5} value={s.hours_after_youtube} onChange={(e) => set('hours_after_youtube', Number(e.target.value))} />
          </Field>
          <Field label="Call to action" hint="Line added to every caption">
            <Input value={s.cta} onChange={(e) => set('cta', e.target.value)} />
          </Field>
          <Field label="Also show in the main feed" hint="share_to_feed — off = Reels tab only">
            <div className="pt-1"><Switch checked={s.share_to_feed} onCheckedChange={(v) => set('share_to_feed', v)} /></div>
          </Field>
        </div>
      )}
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
        <div className={cn('grid grid-cols-2 gap-3', isIg(ch) && 'hidden')}>
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
        {!isIg(ch) && (
          <Field label="Description template" hint="Added after the hook summary and credit line">
            <Textarea rows={3} value={s.description_template} onChange={(e) => set('description_template', e.target.value)} />
          </Field>
        )}
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

/** Per-account publishing mode. Explicit choice beats the global default; auto is never silently downgraded. */
function ModeSelect({ ch }: { ch: Channel }) {
  const qc = useQueryClient()
  const set = useMutation({
    mutationFn: (mode: 'auto' | 'assisted' | 'default') => pub.patchChannel(ch.id, { mode }),
    onSuccess: (c) => {
      toast.success(c.mode === 'auto' ? 'Auto: ClipForge publishes by itself' : 'Assisted: you upload, ClipForge links and tracks')
      qc.invalidateQueries({ queryKey: ['channels'] })
      qc.invalidateQueries({ queryKey: ['queue'] })
      qc.invalidateQueries({ queryKey: ['health'] })
    },
    onError: (e) => toast.error(e.message),
  })
  const manual = !!ch.extra?.manual
  const label = (v: string) => (v === 'default' ? `Default (${ch.mode_source === 'default' ? ch.mode : 'global'})` : v === 'auto' ? 'Auto' : 'Assisted')
  return (
    <div className="flex flex-col gap-1">
      <Select value={manual ? 'assisted' : ch.mode_setting ?? 'default'} disabled={manual || set.isPending}
        onValueChange={(v) => {
          if (v === 'auto' && !window.confirm(`Switch ${ch.title} to auto? ClipForge will ${isIg(ch) ? 'post its Reels at their slots' : 'upload its scheduled clips (private, YouTube publishes them at the slot)'} by itself.`)) return
          set.mutate(v as 'auto' | 'assisted' | 'default')
        }}>
        <SelectTrigger size="sm" className="w-[11.5rem]" aria-label="Publishing mode"><SelectValue>{manual ? 'Assisted (manual account)' : label(ch.mode_setting ?? 'default')}</SelectValue></SelectTrigger>
        <SelectContent>
          <SelectItem value="default">{label('default')}</SelectItem>
          <SelectItem value="auto">Auto</SelectItem>
          <SelectItem value="assisted">Assisted</SelectItem>
        </SelectContent>
      </Select>
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
      toast(c.paused ? `${isIg(ch) ? 'Account' : 'Channel'} paused — nothing will be posted` : 'Active again')
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
    onSuccess: (r) => {
      toast.success(`Disconnected — ${r.revoked}`)
      qc.invalidateQueries({ queryKey: ['channels'] })
    },
    onError: (e) => toast.error(e.message),
  })
  return (
    <Card className="gap-0 py-0">
      <CardContent className="flex flex-col gap-4 p-5 xl:flex-row xl:items-center">
        <div className="flex min-w-0 flex-1 items-center gap-4">
        <ChannelAvatar ch={ch} className="size-12 shrink-0 text-lg" />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2 font-medium">
            <PlatformIcon platform={ch.platform} className="size-4" />
            {isIg(ch) ? `@${ch.title}` : ch.title}
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
          <div className="mt-1 text-xs text-muted-foreground" data-testid="mode-line">
            Mode: <span className="font-medium text-foreground">{ch.mode === 'auto' ? 'auto' : 'assisted'}</span>
            {ch.mode === 'auto' ? (isIg(ch) ? ' — ClipForge posts at each slot' : ' — ClipForge uploads (private + scheduled)') : ' — you upload, ClipForge links and tracks'}
            {ch.mode_problem && <div className="text-destructive">{ch.mode_problem}</div>}
          </div>
          {isIg(ch) && (
            <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              {ch.extra.manual ? (
                <Badge variant="outline">assisted — you post, ClipForge prepares the kit</Badge>
              ) : (
                <>
                  {ch.extra.page_name && <span>Page: {ch.extra.page_name}</span>}
                  {ch.extra.followers != null && <span>· {ch.extra.followers.toLocaleString('en-IN')} followers</span>}
                  {ch.token && <span className={ch.token.ok ? '' : 'text-destructive'}>· {ch.token.text}</span>}
                  <Badge variant="outline" className="gap-1 text-amber-300"><Laptop className="size-3" /> laptop must be on at post times</Badge>
                </>
              )}
            </div>
          )}
        </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {ch.auth_status !== 'ok' && <Button size="sm" onClick={isIg(ch) ? connectInstagram : connect}><RefreshCw /> Reconnect</Button>}
          {!ch.confirmed && <Button size="sm" variant="outline" onClick={() => setConfirmOpen(true)}><ShieldCheck /> Confirm</Button>}
          {!ch.extra?.manual && <Button size="sm" variant="ghost" onClick={() => check.mutate()} disabled={check.isPending}>Test login</Button>}
          <ModeSelect ch={ch} />
          <div className="flex items-center gap-2 px-2 text-sm">
            <Switch checked={!ch.paused} onCheckedChange={(v) => pause.mutate(!v)} id={`p-${ch.id}`} />
            <Label htmlFor={`p-${ch.id}`}>{ch.paused ? 'Paused' : 'Active'}</Label>
          </div>
          <Button size="sm" variant="ghost" className="text-destructive" aria-label="Disconnect and revoke access"
            onClick={() => window.confirm(isIg(ch)
              ? `Disconnect @${ch.title}? ClipForge revokes its access at Meta and deletes the saved login and stats. Reels already posted stay on Instagram.`
              : `Disconnect ${ch.title}? ClipForge revokes its access at Google and deletes the saved login and stats. Videos already on YouTube stay there.`) && del.mutate()}>
            <Unplug /> Disconnect
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

function IgPickDialog({ pid, onDone }: { pid: string; onDone: () => void }) {
  const qc = useQueryClient()
  const { data: options, error } = useQuery({ queryKey: ['ig-pick', pid], queryFn: () => pub.igPickOptions(pid) })
  const pick = useMutation({
    mutationFn: (id: string) => pub.igPick(pid, id),
    onSuccess: (c) => {
      toast.success(`Connected @${c.title} — confirm it before the first post`)
      qc.invalidateQueries({ queryKey: ['channels'] })
      onDone()
    },
    onError: (e) => toast.error(e.message),
  })
  return (
    <Dialog open onOpenChange={(o) => !o && onDone()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Which Instagram account?</DialogTitle>
          <DialogDescription>Instagram professional accounts linked to the Facebook Pages you manage.</DialogDescription>
        </DialogHeader>
        {error ? <ErrorBox error={error} /> : (
          <div className="space-y-2">
            {(options ?? []).map((o) => (
              <button key={o.ig_user_id} onClick={() => pick.mutate(o.ig_user_id)}
                className="flex w-full items-center gap-3 rounded-lg border p-3 text-left hover:border-primary/50">
                <ChannelAvatar ch={{ thumbnail: o.profile_picture_url, title: o.username }} className="size-10" />
                <div>
                  <div className="font-medium">@{o.username}</div>
                  <div className="text-xs text-muted-foreground">{o.followers.toLocaleString('en-IN')} followers · Page: {o.page_name}</div>
                </div>
              </button>
            ))}
          </div>
        )}
      </DialogContent>
    </Dialog>
  )
}

function ManualIgDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const qc = useQueryClient()
  const [name, setName] = useState('')
  const m = useMutation({
    mutationFn: () => pub.igManual(name),
    onSuccess: (c) => {
      toast.success(`Added @${c.title} (assisted)`)
      qc.invalidateQueries({ queryKey: ['channels'] })
      onOpenChange(false)
    },
    onError: (e) => toast.error(e.message),
  })
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Add an Instagram account without a Meta app</DialogTitle>
          <DialogDescription>
            Assisted mode: ClipForge schedules the Reels and makes an <b>Instagram kit</b> (files in order + captions to copy). You post them
            in the Instagram app or schedule them in Meta Business Suite (free), then paste each Reel's link to track it.
          </DialogDescription>
        </DialogHeader>
        <Field label="Instagram username"><Input placeholder="@yourname" value={name} onChange={(e) => setName(e.target.value)} /></Field>
        <DialogFooter><Button onClick={() => m.mutate()} disabled={!name || m.isPending}><UserPlus /> Add</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function InstagramTab({ accounts }: { accounts: Channel[] }) {
  const { data: app } = useMetaApp()
  const [manual, setManual] = useState(false)
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <Button onClick={connectInstagram} disabled={!app?.configured}><Camera /> Add Instagram account</Button>
        <Button variant="outline" onClick={() => setManual(true)}><UserPlus /> Add without a Meta app (assisted)</Button>
      </div>
      {app && !app.configured && (
        <div className="flex items-start gap-3 rounded-xl border border-amber-500/30 bg-amber-500/10 p-4 text-sm">
          <KeyRound className="mt-0.5 size-4 shrink-0 text-amber-300" />
          <div>
            To let ClipForge post Reels by itself, create a free Meta app and paste its App ID + Secret in{' '}
            <Link to="/settings#instagram" className="font-medium text-primary underline">Settings → Instagram</Link>. Needs an Instagram
            <b> professional</b> account linked to a <b>Facebook Page</b>. Or add the account in assisted mode.
          </div>
        </div>
      )}
      {app && (
        <Collapsible>
          <CollapsibleTrigger className="text-xs text-muted-foreground underline">What does ClipForge ask Meta for?</CollapsibleTrigger>
          <CollapsibleContent className="mt-2 space-y-1 text-xs text-muted-foreground">
            {app.scopes.map((s) => <div key={s.scope}><code className="text-foreground">{s.scope}</code> — {s.why}</div>)}
            <div>Instagram has no scheduling in its API: ClipForge uploads ~20 min before each slot and publishes at the slot, so this
              laptop must be on with ClipForge running at post times. A slot missed by more than 90 min is never posted late.</div>
          </CollapsibleContent>
        </Collapsible>
      )}
      {!accounts.length ? (
        <EmptyState icon={Camera} title="No Instagram accounts yet">
          Add your Instagram professional account to post Reels on a schedule, next to your YouTube Shorts.
        </EmptyState>
      ) : <div className="space-y-4">{accounts.map((c) => <ChannelCard key={c.id} ch={c} />)}</div>}
      <ManualIgDialog open={manual} onOpenChange={setManual} />
    </div>
  )
}

export default function Channels() {
  const { data, isLoading, error } = useChannels()
  const { data: status } = usePublishing()
  const [params, setParams] = useSearchParams()
  const pid = params.get('pick')
  const igPid = params.get('ig_pick')
  const [tab, setTab] = useState(params.get('tab') === 'instagram' ? 'instagram' : 'youtube')
  useEffect(() => {
    if (params.get('connected')) {
      toast.success('Channel connected — confirm it before the first upload')
      setParams({}, { replace: true })
    }
    if (params.get('error')) {
      toast.error(`Sign-in failed: ${params.get('error')}`)
      setParams({}, { replace: true })
    }
  }, [params, setParams])
  const noSecret = status && !status.client_secret
  const yt = (data ?? []).filter((c) => !isIg(c))
  const igs = (data ?? []).filter(isIg)
  const remote = !['localhost', '127.0.0.1', '[::1]'].includes(window.location.hostname)
  return (
    <>
      <PageHeader
        title="Accounts"
        description="YouTube channels and Instagram accounts ClipForge posts to. Logins stay on this computer."
        actions={tab === 'youtube' ? <Button onClick={connect} disabled={!!noSecret || remote}><Plus /> Add channel</Button> : undefined}
      />
      {remote && (
        <div className="mb-5 flex items-start gap-3 rounded-xl border border-sky-500/30 bg-sky-500/10 p-4 text-sm">
          <Laptop className="mt-0.5 size-4 shrink-0" />
          <div>Connect accounts on the laptop: Google and Meta send logins back to <code>localhost</code>. Everything else works from here.</div>
        </div>
      )}
      <Tabs value={tab} onValueChange={setTab}>
        <TabsList className="mb-4">
          <TabsTrigger value="youtube"><PlatformIcon platform="youtube" /> YouTube ({yt.length})</TabsTrigger>
          <TabsTrigger value="instagram"><PlatformIcon platform="instagram" /> Instagram ({igs.length})</TabsTrigger>
        </TabsList>
        <TabsContent value="instagram">{error ? <ErrorBox error={error} /> : <InstagramTab accounts={igs} />}</TabsContent>
        <TabsContent value="youtube">
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
      {error ? <ErrorBox error={error} /> : isLoading ? <Skeleton className="h-40 rounded-xl" /> : !yt.length ? (
        <EmptyState icon={Tv} title="No channels yet" action={<Button onClick={connect} disabled={!!noSecret || remote}><Plus /> Add channel</Button>}>
          Connect the YouTube channel(s) you post Shorts to. You can add several (Brand Accounts too).
        </EmptyState>
      ) : (
        <div className="space-y-4">{yt.map((c) => <ChannelCard key={c.id} ch={c} />)}</div>
      )}
        </TabsContent>
      </Tabs>
      {pid && <PickDialog pid={pid} onDone={() => setParams({}, { replace: true })} />}
      {igPid && <IgPickDialog pid={igPid} onDone={() => setParams({ tab: 'instagram' }, { replace: true })} />}
      {status?.worker.current && (
        <div className="mt-4 flex items-center gap-2 text-xs text-muted-foreground"><Loader2 className="size-3 animate-spin" /> {status.worker.current}</div>
      )}
    </>
  )
}
