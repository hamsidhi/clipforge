import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Clapperboard, Cpu, Gamepad2, Pause, Play, Radio, Scissors, Trash2, Upload, Clock } from 'lucide-react'
import { toast } from 'sonner'
import { live, type LiveClip, type LiveSession, type LiveStatus } from '@/api/live'
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

function Dot({ ok, label }: { ok: boolean | undefined; label: string }) {
  return (
    <div className="flex items-center gap-2 rounded-lg border px-3 py-2 text-sm">
      <span className={`size-2.5 shrink-0 rounded-full ${ok ? 'bg-emerald-500' : 'bg-muted-foreground/40'}`} />
      <span className="truncate">{label}</span>
    </div>
  )
}

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
  return (
    <section className="space-y-4 rounded-xl border p-4">
      <h2 className="font-medium">Settings</h2>
      <div className="flex items-center justify-between gap-4">
        <div><Label>Live mode</Label><p className="text-xs text-muted-foreground">Watch OBS / your stream and clip automatically</p></div>
        <Switch checked={Boolean(v.enabled)} onCheckedChange={(x) => set('enabled', x)} />
      </div>
      <div className="flex items-center justify-between gap-4">
        <div><Label>Auto-post</Label><p className="text-xs text-muted-foreground">Post high-confidence clips (manual + game events) by themselves</p></div>
        <Switch checked={Boolean(v.auto_post)} onCheckedChange={(x) => set('auto_post', x)} />
      </div>
      <div className="flex items-center justify-between gap-4">
        <div><Label>Captions</Label><p className="text-xs text-muted-foreground">Skipped automatically when the CPU is busy</p></div>
        <Switch checked={Boolean(v.captions)} onCheckedChange={(x) => set('captions', x)} />
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <div className="space-y-1">
          <Label htmlFor="source">Footage</Label>
          <select id="source" className="h-9 w-full rounded-md border bg-background px-2 text-sm" value={String(v.source)} onChange={(e) => set('source', e.target.value)}>
            <option value="obs">OBS Replay Buffer (this PC)</option>
            <option value="hls">My stream link (no OBS)</option>
          </select>
        </div>
        <div className="space-y-1">
          <Label htmlFor="game">Game</Label>
          <select id="game" className="h-9 w-full rounded-md border bg-background px-2 text-sm" value={String(v.game_pack)} onChange={(e) => set('game_pack', e.target.value)}>
            {GAMES.map((g) => <option key={g} value={g}>{g === 'auto' ? 'Detect from OBS' : g}</option>)}
          </select>
        </div>
        <div className="space-y-1 sm:col-span-2">
          <Label htmlFor="urls">My stream links (one per line)</Label>
          <Textarea id="urls" rows={2} placeholder="https://www.youtube.com/@you&#10;https://www.twitch.tv/you"
            value={((v.stream_urls as string[]) ?? []).join('\n')} onChange={(e) => set('stream_urls', e.target.value.split('\n').map((x) => x.trim()).filter(Boolean))} />
        </div>
        <div className="space-y-1">
          <Label htmlFor="twitch">Twitch chat channel</Label>
          <Input id="twitch" placeholder="yourname" value={String(v.twitch_channel ?? '')} onChange={(e) => set('twitch_channel', e.target.value)} />
        </div>
        <div className="flex items-center justify-between gap-4 rounded-md border px-3">
          <Label>Read YouTube live chat (uses API quota)</Label>
          <Switch checked={Boolean(v.youtube_chat)} onCheckedChange={(x) => set('youtube_chat', x)} />
        </div>
        {num('cooldown_sec', 'Cooldown (s)', 'Min gap between automatic clips')}
        {num('max_clips_per_hour', 'Max clips / hour')}
        {num('pre_roll', 'Before the moment (s)')}
        {num('post_roll', 'After the moment (s)')}
        {num('min_score', 'Min score (0–10)')}
        {num('auto_post_min_score', 'Auto-post min score')}
        {num('min_post_gap_min', 'Post gap while live (min)', 'Per platform')}
        {num('cpu_ceiling', 'CPU ceiling (%)', 'Above this for 5 s → clipping pauses')}
        {num('max_cores', 'CPU cores for ClipForge')}
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
      <h2 className="font-medium">Gaming accounts</h2>
      <p className="text-xs text-muted-foreground">Live clips only go to accounts switched on here — never to your podcast accounts.</p>
      {(data ?? []).length === 0 && <p className="text-sm text-muted-foreground">No accounts yet — connect your gaming YouTube / Instagram on the Accounts page.</p>}
      <ul className="divide-y">
        {(data ?? []).map((a) => (
          <li key={a.id} className="flex items-center gap-3 py-2 text-sm">
            <PlatformIcon platform={a.platform} />
            <span className="min-w-0 flex-1 truncate">{a.title}{!a.ok && <span className="text-muted-foreground"> · needs attention</span>}</span>
            <Switch checked={a.gaming} onCheckedChange={(g) => set.mutate({ id: a.id, g })} aria-label={`Gaming account: ${a.title}`} />
          </li>
        ))}
      </ul>
    </section>
  )
}

/** Live gaming: highlight clips while you stream (clipforge/live). */
export default function Live() {
  const qc = useQueryClient()
  const { data, isLoading, error } = useQuery({ queryKey: ['live', 'status'], queryFn: live.status, refetchInterval: 1500 })
  const clip = useMutation({
    mutationFn: () => live.clip(),
    onSuccess: () => toast.success('Marked! The clip renders in 1–3 minutes.'),
    onError: (e) => toast.error((e as Error).message),
  })
  const control = useMutation({
    mutationFn: live.control,
    onSuccess: () => qc.invalidateQueries({ queryKey: ['live'] }),
    onError: (e) => toast.error((e as Error).message),
  })
  const best = useMutation({
    mutationFn: live.bestof,
    onSuccess: (r) => toast.success(r.message),
    onError: (e) => toast.error((e as Error).message),
  })
  if (error) return <ErrorBox error={error} />
  if (isLoading || !data) return <div className="space-y-3">{[0, 1, 2].map((i) => <Skeleton key={i} className="h-28 rounded-xl" />)}</div>
  const st = data.status
  const lv = st.live
  const gov = lv?.governor ?? st.governor
  const isLive = Boolean(st.engine_running && lv?.session)
  return (
    <>
      <PageHeader title="Live"
        description={isLive ? `🔴 Live · ${lv?.game_label}` : st.engine_running ? (st.note ?? 'Waiting for the stream') : data.enabled ? 'Starting the live agent…' : 'Live mode is off'}
        actions={
          <Button size="lg" onClick={() => clip.mutate()} disabled={!isLive || clip.isPending} className="gap-2">
            <Scissors className="size-5" /> CLIP NOW
          </Button>
        } />
      <div className="space-y-4">
        <section className="grid grid-cols-2 gap-2 sm:grid-cols-3">
          <Dot ok={st.engine_running} label="Live agent" />
          <Dot ok={st.obs?.connected} label="OBS connected" />
          <Dot ok={st.obs?.streaming || isLive} label="Streaming" />
          <Dot ok={st.obs?.replay} label="Replay buffer" />
          <Dot ok={Boolean(lv?.game && lv.game !== 'generic')} label={`Game: ${lv?.game_label ?? '—'}`} />
          <Dot ok={lv?.facecam} label="Facecam found" />
        </section>
        {gov && (
          <section className="space-y-2 rounded-xl border p-4">
            <div className="flex items-center gap-2 text-sm font-medium"><Cpu className="size-4" /> CPU & stream health
              <Badge variant={gov.state === 'ok' ? 'secondary' : 'destructive'} className="ml-auto">{gov.state}</Badge></div>
            <Meter label="Total CPU" value={gov.cpu} text={`${Math.round(gov.cpu)}%`} warn={gov.state !== 'ok'} />
            {gov.reason && <p className="text-xs text-amber-600">{gov.reason}</p>}
            <p className="text-xs text-muted-foreground">Encoder: {data.amf ? 'AMD AMF on the iGPU (CPU stays free)' : 'x264 ultrafast (no AMF found)'}</p>
          </section>
        )}
        {lv && (
          <section className="space-y-3 rounded-xl border p-4">
            <div className="flex flex-wrap items-center gap-2">
              <Radio className="size-4 text-red-500" /><span className="font-medium">Signals</span>
              <div className="ml-auto flex gap-2">
                <Button size="sm" variant="outline" onClick={() => control.mutate(lv.paused ? 'resume' : 'pause')}>
                  {lv.paused ? <><Play className="size-4" /> Resume</> : <><Pause className="size-4" /> Pause</>}
                </Button>
                <Button size="sm" variant={lv.auto_post ? 'default' : 'outline'} onClick={() => control.mutate(lv.auto_post ? 'auto_off' : 'auto_on')}>
                  Auto-post {lv.auto_post ? 'on' : 'off'}
                </Button>
              </div>
            </div>
            <Meter label="Mic" value={pct(lv.meters.mic)} text={`${Math.round(lv.meters.mic)} dB`} />
            <Meter label="Game audio" value={pct(lv.meters.desktop)} text={`${Math.round(lv.meters.desktop)} dB`} />
            <Meter label="Chat" value={Math.min(100, lv.meters.chat_rate * 5)} text={`${lv.meters.chat_rate} msgs / 10 s`} />
            <Meter label="Moment score" value={lv.meters.score * 10} text={`${lv.meters.score} (needs > ${lv.meters.threshold})`} />
            {lv.hud_events.length > 0 && (
              <p className="text-xs text-muted-foreground">Last game events: {lv.hud_events.slice(-4).map((e) => e.note).join(' · ')}</p>
            )}
            <p className="text-xs text-muted-foreground">{lv.clips_made} clips · {lv.rendering} rendering · {lv.queued} waiting</p>
          </section>
        )}
        {lv && lv.moments.length > 0 && (
          <section className="rounded-xl border">
            <h2 className="border-b px-4 py-2 font-medium">Moments</h2>
            <ul className="divide-y text-sm">
              {[...lv.moments].reverse().map((m) => (
                <li key={m.id} className="flex gap-3 px-4 py-2">
                  <span className="w-14 shrink-0 tabular-nums text-muted-foreground">{Math.floor(m.t / 60)}:{String(Math.floor(m.t % 60)).padStart(2, '0')}</span>
                  <span className="min-w-0 flex-1">{m.reason}</span>
                  <span className="shrink-0 text-xs text-muted-foreground">⭐ {m.score} · {m.state}</span>
                </li>
              ))}
            </ul>
          </section>
        )}
        {data.sessions.map((s) => (
          <section key={s.id} className="overflow-hidden rounded-xl border">
            <header className="flex flex-wrap items-center gap-2 bg-muted/40 px-4 py-2">
              <Gamepad2 className="size-4" />
              <span className="font-medium">{s.game_label} · {s.started_at?.replace('T', ' ').slice(0, 16)}</span>
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
