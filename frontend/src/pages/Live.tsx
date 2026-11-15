import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Clapperboard, Clock, Cpu, Gamepad2, Pause, Play, Radio, Scissors, Square, Trash2, Upload } from 'lucide-react'
import { toast } from 'sonner'
import { live, type LiveClip, type LiveSession, type LiveStatus, type LiveStream } from '@/api/live'
import { ErrorBox, PageHeader } from '@/components/common'
import PlatformIcon from '@/components/PlatformIcon'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { Switch } from '@/components/ui/switch'
import { Textarea } from '@/components/ui/textarea'

const GAMES = ['auto', 'bgmi_pubg', 'valorant', 'cs2', 'free_fire', 'gta5', 'gta6', 'minecraft', 'generic']
const CAMERA: Record<string, string> = { facecam: 'facecam found', irl: 'face (IRL)', none: 'no facecam', checking: 'looking for facecam…' }

/** dBFS (-60..0) → 0..100 % */
const pct = (db: number) => Math.max(0, Math.min(100, ((db + 60) / 60) * 100))

function Meter({ label, value, text, warn }: { label: string; value: number; text: string; warn?: boolean }) {
  return (
    <div className="space-y-1">
      <div className="flex justify-between text-xs text-muted-foreground"><span>{label}</span><span className="tabular-nums">{text}</span></div>
      <div className="h-2 overflow-hidden rounded-full bg-muted">
        <div className={`h-full rounded-full transition-all ${warn ? 'bg-amber-500' : 'bg-primary'}`} style={{ width: `${value}%` }} />
      </div>
    </div>
  )
}

function StreamCard({ s }: { s: LiveStream }) {
  const qc = useQueryClient()
  const done = (msg?: string) => { if (msg) toast.success(msg); qc.invalidateQueries({ queryKey: ['live'] }) }
  const err = (e: unknown) => toast.error((e as Error).message)
  const clip = useMutation({ mutationFn: () => live.clip(s.session), onSuccess: () => done('Marked — the clip arrives in about a minute.'), onError: err })
  const ctl = useMutation({ mutationFn: (a: 'pause' | 'resume' | 'auto_on' | 'auto_off') => live.control(s.session, a), onSuccess: () => done(), onError: err })
  const stop = useMutation({ mutationFn: () => live.stop(s.session), onSuccess: () => done('Stopping — clips in progress finish first.'), onError: err })
  const m = s.meters
  return (
    <section className="space-y-3 rounded-xl border p-4">
      <div className="flex flex-wrap items-center gap-2">
        <Radio className="size-4 text-red-500" />
        <a href={s.url} target="_blank" rel="noreferrer" className="font-medium hover:underline">{s.streamer || s.session}</a>
        <Badge variant="secondary">{s.platform}</Badge>
        <Badge variant="outline">{s.game_label}</Badge>
        {s.ending && <Badge variant="outline">ending…</Badge>}
        {s.paused && <Badge variant="outline">paused</Badge>}
      </div>
      {s.title && <p className="truncate text-xs text-muted-foreground">{s.title}</p>}
      <div className="flex flex-wrap gap-2">
        <Button onClick={() => clip.mutate()} disabled={clip.isPending || s.ending}><Scissors className="size-4" /> CLIP NOW</Button>
        <Button variant="outline" onClick={() => ctl.mutate(s.paused ? 'resume' : 'pause')} disabled={s.ending}>
          {s.paused ? <><Play className="size-4" /> Resume</> : <><Pause className="size-4" /> Pause</>}
        </Button>
        <Button variant={s.auto_post ? 'default' : 'outline'} onClick={() => ctl.mutate(s.auto_post ? 'auto_off' : 'auto_on')} disabled={s.ending}>
          Auto-post {s.auto_post ? 'on' : 'off'}
        </Button>
        <Button variant="ghost" onClick={() => stop.mutate()} disabled={stop.isPending || s.ending}><Square className="size-4" /> Stop</Button>
      </div>
      <Meter label="Stream audio" value={pct(m.audio)} text={`${Math.round(m.audio)} dB`} />
      <Meter label="Chat" value={Math.min(100, m.chat_rate * 2)} text={`${m.chat_rate} msgs / 10 s`} />
      <Meter label="Moment score" value={m.score * 10} text={`${m.score} (needs > ${m.threshold})`} />
      <p className="text-xs text-muted-foreground">
        Camera: {CAMERA[s.camera] ?? s.camera} · {s.clips_made} clips · {s.rendering} rendering · {s.queued} waiting
        {s.hud_events.length > 0 && <> · game events: {s.hud_events.slice(-3).map((e) => e.note).join(', ')}</>}
      </p>
      {s.moments.length > 0 && (
        <ul className="divide-y rounded-lg border text-sm">
          {[...s.moments].reverse().slice(0, 6).map((mo) => (
            <li key={mo.id} className="flex gap-3 px-3 py-1.5">
              <span className="w-12 shrink-0 tabular-nums text-muted-foreground">{Math.floor(mo.t / 60)}:{String(Math.floor(mo.t % 60)).padStart(2, '0')}</span>
              <span className="min-w-0 flex-1 truncate">{mo.reason}</span>
              <span className="shrink-0 text-xs text-muted-foreground">⭐ {mo.score} · {mo.state}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

function ClipRow({ s, c }: { s: LiveSession; c: LiveClip }) {
  const qc = useQueryClient()
  const act = useMutation({
    mutationFn: (what: 'now' | 'queue' | 'discard') => what === 'discard' ? live.discard(s.id, c.rank).then(() => ({ lines: ['Discarded'] }))
      : live.post(s.id, c.rank, what),
    onSuccess: (r) => { toast.success(r.lines.join(' · ')); qc.invalidateQueries({ queryKey: ['live'] }) },
    onError: (e) => toast.error((e as Error).message),
  })
  const done = c.status === 'discarded' || Object.keys(c.posted ?? {}).length > 0
  return (
    <li className="flex gap-3 p-3">
      <a href={live.video(s.id, c.rank)} target="_blank" rel="noreferrer" className="shrink-0">
        <img src={live.thumb(s.id, c.rank)} alt="" className="h-24 w-[54px] rounded-md bg-muted object-cover" loading="lazy"
          onError={(e) => { (e.target as HTMLImageElement).style.visibility = 'hidden' }} />
      </a>
      <div className="min-w-0 flex-1 space-y-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-medium">#{c.rank} {c.title}</span>
          <Badge variant="secondary">⭐ {c.score.toFixed(1)}</Badge>
          {c.status !== 'ready' && <Badge variant="outline">{c.status}</Badge>}
        </div>
        <p className="text-xs text-muted-foreground">{c.reason}</p>
        <p className="text-xs text-muted-foreground">
          {Math.round(c.duration)} s · ready in {c.render?.total_seconds ?? '?'} s ({c.render?.encoder}) · {c.captions}
        </p>
        {!done && !s.simulated && (
          <div className="flex flex-wrap gap-2 pt-1">
            <Button size="sm" onClick={() => act.mutate('now')} disabled={act.isPending}><Upload className="size-4" /> Post now</Button>
            <Button size="sm" variant="outline" onClick={() => act.mutate('queue')} disabled={act.isPending}><Clock className="size-4" /> Queue</Button>
            <Button size="sm" variant="ghost" onClick={() => act.mutate('discard')} disabled={act.isPending}><Trash2 className="size-4" /> Discard</Button>
          </div>
        )}
      </div>
    </li>
  )
}

function Settings({ data }: { data: LiveStatus }) {
  const qc = useQueryClient()
  const [v, setV] = useState<Record<string, unknown>>(data.settings)
  useEffect(() => setV(data.settings), [data.settings])
  const set = (k: string, val: unknown) => setV((o) => ({ ...o, [k]: val }))
  const save = useMutation({
    mutationFn: () => live.saveSettings(v),
    onSuccess: () => { toast.success('Live settings saved'); qc.invalidateQueries({ queryKey: ['live'] }) },
    onError: (e) => toast.error((e as Error).message),
  })
  const num = (k: string, label: string, hint?: string) => (
    <div className="space-y-1">
      <Label htmlFor={k}>{label}</Label>
      <Input id={k} type="number" value={String(v[k] ?? '')} onChange={(e) => set(k, Number(e.target.value))} />
      {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
    </div>
  )
  const sw = (k: string, label: string, hint: string) => (
    <div className="flex items-center justify-between gap-4">
      <div><Label>{label}</Label><p className="text-xs text-muted-foreground">{hint}</p></div>
      <Switch checked={Boolean(v[k])} onCheckedChange={(x) => set(k, x)} aria-label={label} />
    </div>
  )
  return (
    <section className="space-y-4 rounded-xl border p-4">
      <h2 className="font-medium">Settings</h2>
      {sw('enabled', 'Live clipping', 'Run the live agent with the app (idle until you add a stream)')}
      {sw('auto_post', 'Auto-post', 'Post high-confidence clips by themselves (off = drafts you approve)')}
      {sw('captions', 'Captions', 'Skipped automatically when the CPU is busy')}
      {sw('chat_twitch', 'Read Twitch chat', 'Free, anonymous')}
      {sw('chat_kick', 'Read Kick chat', 'Free, best effort')}
      {sw('chat_youtube', 'Read YouTube chat', "Uses your API quota; stops early to keep quota for uploads")}
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="space-y-1 sm:col-span-2">
          <Label htmlFor="watch">Auto-start when these channels go live (one per line)</Label>
          <Textarea id="watch" rows={2} placeholder="https://www.twitch.tv/name&#10;https://kick.com/name"
            value={((v.watch_channels as string[]) ?? []).join('\n')}
            onChange={(e) => set('watch_channels', e.target.value.split('\n').map((x) => x.trim()).filter(Boolean))} />
        </div>
        <div className="space-y-1">
          <Label htmlFor="game">Game</Label>
          <select id="game" className="h-9 w-full rounded-md border bg-background px-2 text-sm" value={String(v.game_pack)} onChange={(e) => set('game_pack', e.target.value)}>
            {GAMES.map((g) => <option key={g} value={g}>{g === 'auto' ? 'Detect from the stream title' : g}</option>)}
          </select>
        </div>
        {num('max_streams', 'Streams at the same time')}
        {num('cooldown_sec', 'Cooldown (s)', 'Min gap between automatic clips')}
        {num('max_clips_per_hour', 'Max clips / hour (per stream)')}
        {num('pre_roll', 'Before the moment (s)')}
        {num('post_roll', 'After the moment (s)')}
        {num('min_score', 'Min score (0–10)')}
        {num('auto_post_min_score', 'Auto-post min score')}
        {num('min_post_gap_min', 'Post gap while live (min)', 'Per platform')}
        {num('cpu_ceiling', 'CPU ceiling (%)', 'Above this for 5 s → live work waits')}
        {num('max_cores', 'CPU cores for live clipping')}
      </div>
      <Button onClick={() => save.mutate()} disabled={save.isPending}>Save</Button>
    </section>
  )
}

function Accounts() {
  const qc = useQueryClient()
  const { data } = useQuery({ queryKey: ['live', 'accounts'], queryFn: live.accounts })
  const set = useMutation({
    mutationFn: ({ id, g }: { id: string; g: boolean }) => live.setGaming(id, g),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['live'] }),
    onError: (e) => toast.error((e as Error).message),
  })
  return (
    <section className="space-y-3 rounded-xl border p-4">
      <h2 className="font-medium">Accounts for live clips</h2>
      <p className="text-xs text-muted-foreground">Post now / Queue / Auto-post only go to accounts switched on here.</p>
      {(data ?? []).length === 0 && <p className="text-sm text-muted-foreground">No accounts yet — connect one on the Accounts page.</p>}
      <ul className="divide-y">
        {(data ?? []).map((a) => (
          <li key={a.id} className="flex items-center gap-3 py-2 text-sm">
            <PlatformIcon platform={a.platform} />
            <span className="min-w-0 flex-1 truncate">{a.title}{!a.ok && <span className="text-muted-foreground"> · needs attention</span>}</span>
            <Switch checked={a.gaming} onCheckedChange={(g) => set.mutate({ id: a.id, g })} aria-label={`Live clips on ${a.title}`} />
          </li>
        ))}
      </ul>
    </section>
  )
}

/** Live clipping: paste a live stream link → highlight clips while it's live (clipforge/live). */
export default function Live() {
  const qc = useQueryClient()
  const [url, setUrl] = useState('')
  const { data, isLoading, error } = useQuery({ queryKey: ['live', 'status'], queryFn: live.status, refetchInterval: 2000 })
  const start = useMutation({
    mutationFn: () => live.start(url),
    onSuccess: (r) => { toast.success(r.message); setUrl(''); qc.invalidateQueries({ queryKey: ['live'] }) },
    onError: (e) => toast.error((e as Error).message),
  })
  const best = useMutation({ mutationFn: live.bestof, onSuccess: (r) => toast.success(r.message), onError: (e) => toast.error((e as Error).message) })
  if (error) return <ErrorBox error={error} />
  if (isLoading || !data) return <div className="space-y-3">{[0, 1, 2].map((i) => <Skeleton key={i} className="h-28 rounded-xl" />)}</div>
  const st = data.status
  const streams = st.engine_running ? (st.streams ?? []) : []
  const gov = st.governor
  return (
    <>
      <PageHeader title="Live"
        description={!data.enabled ? 'Live clipping is off (settings below)' : !st.engine_running ? 'Starting the live agent…'
          : streams.length ? `Watching ${streams.length} stream${streams.length > 1 ? 's' : ''}` : 'Paste a live stream link to start'} />
      <div className="space-y-4">
        <form className="flex flex-col gap-2 rounded-xl border p-4 sm:flex-row" onSubmit={(e) => { e.preventDefault(); if (url.trim()) start.mutate() }}>
          <Input placeholder="Live stream link — twitch.tv/…, kick.com/…, youtube.com/…/live" value={url} onChange={(e) => setUrl(e.target.value)}
            aria-label="Live stream link" />
          <Button type="submit" disabled={!url.trim() || start.isPending}><Radio className="size-4" /> Start clipping</Button>
        </form>
        <p className="-mt-2 px-1 text-xs text-muted-foreground">
          Clips of other creators can get copyright claims — every caption credits and links the streamer. Ask for permission when you can.
        </p>
        {(st.starting ?? []).map((u) => <p key={u} className="text-sm text-muted-foreground">⏳ Opening {u}…</p>)}
        {(st.errors ?? []).slice(-2).map((e, i) => <p key={i} className="text-sm text-amber-600">⚠️ {e}</p>)}
        {gov && streams.length > 0 && (
          <section className="space-y-2 rounded-xl border p-4">
            <div className="flex items-center gap-2 text-sm font-medium"><Cpu className="size-4" /> Laptop load
              <Badge variant={gov.state === 'ok' ? 'secondary' : 'destructive'} className="ml-auto">{gov.state}</Badge></div>
            <Meter label="Total CPU" value={gov.cpu} text={`${Math.round(gov.cpu)}%`} warn={gov.state !== 'ok'} />
            {gov.reason && <p className="text-xs text-amber-600">{gov.reason}</p>}
            <p className="text-xs text-muted-foreground">Encoder: {data.amf ? 'AMD AMF — decode + encode on the iGPU' : 'x264 ultrafast (no AMF found)'}</p>
          </section>
        )}
        {streams.map((s) => <StreamCard key={s.session} s={s} />)}
        {data.sessions.map((s) => (
          <section key={s.id} className="overflow-hidden rounded-xl border">
            <header className="flex flex-wrap items-center gap-2 bg-muted/40 px-4 py-2">
              <Gamepad2 className="size-4" />
              <span className="font-medium">{s.streamer || s.game_label} · {s.started_at?.replace('T', ' ').slice(0, 16)}</span>
              {s.live && <Badge variant="destructive">LIVE</Badge>}
              {s.simulated && <Badge variant="outline">simulation</Badge>}
              <span className="ml-auto text-xs text-muted-foreground">{s.clips.length} clips</span>
              {!s.live && s.clips.length > 0 && (s.best_of
                ? <a className="text-xs underline" href={`/api/live/sessions/${s.id}/bestof/video`} target="_blank" rel="noreferrer">Highlight video</a>
                : <Button size="sm" variant="outline" onClick={() => best.mutate(s.id)}><Clapperboard className="size-4" /> Make highlight video</Button>)}
            </header>
            {s.clips.length === 0 ? <p className="px-4 py-3 text-sm text-muted-foreground">No clips yet.</p> : (
              <ul className="divide-y">{[...s.clips].reverse().map((c) => <ClipRow key={c.rank} s={s} c={c} />)}</ul>
            )}
          </section>
        ))}
        <Accounts />
        <Settings data={data} />
      </div>
    </>
  )
}
