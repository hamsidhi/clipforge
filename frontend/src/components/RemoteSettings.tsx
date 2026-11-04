import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Bot, KeyRound, Laptop, Loader2, LogOut, Shield, Smartphone } from 'lucide-react'
import { toast } from 'sonner'
import { dataSaver, isRemote, request } from '@/api/client'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'

type Cfg = Record<string, Record<string, unknown>>
interface Remote {
  installed: boolean; running: boolean; dns_name: string; magic_dns: boolean; https_certs: boolean; serving: boolean; url: string
  problems: string[]; steps: string[]; local: boolean; password_set: boolean; here: string
}
interface TgDevice { chat_id: number; name: string; role: 'owner' | 'viewer'; kind: string; paired_at: string; last_seen: string | null }
interface Tg {
  token: string | null; paired: boolean; enabled: boolean; running: boolean; polling?: boolean; bot_username?: string
  pin_set?: boolean; pin_required?: boolean; devices?: TgDevice[]; mode?: string
}
interface Sess { id: number; device: string; ip: string; last_seen: string; expires_at: string; current: boolean }
interface Ev { at: string; kind: string; detail: string; ip: string }

const TOGGLES: [string, string][] = [
  ['notify_job_done', 'Job finished (clip cards: preview, approve, edit, schedule)'], ['notify_job_failed', 'Job failed'],
  ['notify_published', 'Posted on YouTube / Instagram'], ['notify_publish_failed', 'Upload failed or Instagram slot missed'],
  ['notify_reauth', 'An account needs re-login'], ['notify_quota', 'YouTube quota used up'], ['daily_summary', 'Daily summary'],
]

function Section({ icon: Icon, title, children }: { icon: typeof Bot; title: string; children: React.ReactNode }) {
  return (
    <div className="space-y-3 rounded-xl border p-4">
      <div className="flex items-center gap-2 font-medium"><Icon className="size-4" /> {title}</div>
      {children}
    </div>
  )
}

export default function RemoteSettings({ cfg, set }: { cfg: Cfg; set: (section: string, key: string, value: unknown) => void }) {
  const qc = useQueryClient()
  const remote = useQuery({ queryKey: ['remote'], queryFn: () => request<Remote>('/api/remote/status') })
  const tg = useQuery({ queryKey: ['tg'], queryFn: () => request<Tg>('/api/telegram') })
  const sessions = useQuery({ queryKey: ['sessions'], queryFn: () => request<Sess[]>('/api/auth/sessions') })
  const audit = useQuery({ queryKey: ['audit'], queryFn: () => request<Ev[]>('/api/auth/audit') })
  const [pw, setPw] = useState('')
  const [cur, setCur] = useState('')
  const [token, setToken] = useState('')
  const [code, setCode] = useState<string | null>(null)
  const [codeRole, setCodeRole] = useState<'owner' | 'viewer'>('owner')
  const [pin, setPin] = useState('')
  const [saver, setSaver] = useState(dataSaver())
  const r = remote.data
  const post = (path: string, body?: unknown) => request<Record<string, unknown>>(path, { method: 'POST', body: body ? JSON.stringify(body) : undefined })
  const setPassword = useMutation({
    mutationFn: () => post('/api/auth/setup', { password: pw, current: cur || undefined }),
    onSuccess: () => {
      toast.success('Password saved — every device logs in again')
      setPw('')
      setCur('')
      qc.invalidateQueries({ queryKey: ['remote'] })
    },
    onError: (e) => toast.error(e.message),
  })
  const setup = useMutation({
    mutationFn: () => post('/api/remote/setup'),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['remote'] }),
    onError: (e) => toast.error(e.message),
  })
  const saveToken = useMutation({
    mutationFn: () => post('/api/telegram/token', { token }),
    onSuccess: (d) => {
      toast.success(`Bot @${String(d.bot_username)} saved — now pair it`)
      setToken('')
      qc.invalidateQueries({ queryKey: ['tg'] })
    },
    onError: (e) => toast.error(e.message),
  })
  const pair = useMutation({
    mutationFn: (role: 'owner' | 'viewer') => post('/api/telegram/pair', { role }),
    onSuccess: (d) => { setCode(String(d.code)); setCodeRole(d.role === 'viewer' ? 'viewer' : 'owner') },
    onError: (e) => toast.error(e.message),
  })
  const savePin = useMutation({
    mutationFn: (body: { pin?: string; required?: boolean }) => post('/api/telegram/pin', body),
    onSuccess: () => { setPin(''); toast.success('Saved'); qc.invalidateQueries({ queryKey: ['tg'] }) },
    onError: (e) => toast.error(e.message),
  })
  const unpairOne = useMutation({
    mutationFn: (chat_id: number) => post('/api/telegram/unpair', { chat_id }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['tg'] }),
    onError: (e) => toast.error(e.message),
  })
  const test = useMutation({ mutationFn: () => post('/api/telegram/test'), onSuccess: () => toast.success('Sent — check Telegram'), onError: (e) => toast.error(e.message) })
  const unpair = useMutation({ mutationFn: () => post('/api/telegram/unpair'), onSuccess: () => qc.invalidateQueries({ queryKey: ['tg'] }) })
  const revoke = useMutation({
    mutationFn: (id: number) => request(`/api/auth/sessions/${id}`, { method: 'DELETE' }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['sessions'] }),
  })
  const t = cfg.telegram
  return (
    <Card id="remote">
      <CardHeader>
        <CardTitle className="flex items-center gap-2"><Smartphone className="size-5" /> Phone &amp; remote</CardTitle>
        <CardDescription>Use ClipForge from your phone anywhere — privately through Tailscale, never open to the internet.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-5">
        <Section icon={KeyRound} title={r?.password_set ? 'Password (set)' : 'Password — needed before using it from the phone'}>
          <div className="grid gap-3 sm:grid-cols-3">
            {isRemote() && <Input type="password" placeholder="Current password" value={cur} onChange={(e) => setCur(e.target.value)} />}
            <Input type="password" autoComplete="new-password" placeholder="New password (10+ characters)" value={pw} onChange={(e) => setPw(e.target.value)} />
            <Button onClick={() => setPassword.mutate()} disabled={pw.length < 10 || setPassword.isPending}>{r?.password_set ? 'Change password' : 'Set password'}</Button>
          </div>
          <p className="text-xs text-muted-foreground">Stored only as a scrypt hash in <code>.env</code>. On this laptop (localhost) you never need it.</p>
        </Section>

        <Section icon={Laptop} title="Remote access (Tailscale)">
          {!r ? <Loader2 className="size-4 animate-spin" /> : !r.local ? (
            <p className="text-sm text-muted-foreground">You're connected remotely ({r.here}). Remote access is managed on the laptop.</p>
          ) : (
            <>
              <div className="grid gap-1 text-sm sm:grid-cols-2">
                <div>Tailscale installed: <b>{r.installed ? 'yes' : 'no'}</b></div>
                <div>Logged in: <b>{r.running ? 'yes' : 'no'}</b></div>
                <div>MagicDNS: <b>{r.magic_dns ? 'on' : 'off'}</b></div>
                <div>HTTPS certificates: <b>{r.https_certs ? 'on' : 'off'}</b></div>
                <div className="sm:col-span-2">Serving ClipForge to your tailnet: <b>{r.serving ? 'yes' : 'no'}</b></div>
              </div>
              {r.steps.length > 0 && (
                <ol className="list-decimal space-y-1 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 pl-7 text-sm">
                  {r.steps.map((s) => <li key={s}>{s}</li>)}
                </ol>
              )}
              <div className="flex flex-wrap gap-2">
                <Button onClick={() => setup.mutate()} disabled={!r.installed || !r.password_set || setup.isPending}>
                  {setup.isPending ? <Loader2 className="animate-spin" /> : <Smartphone />} {r.serving ? 'Re-check' : 'Set up remote access'}
                </Button>
                <Button variant="ghost" asChild><a href="https://tailscale.com/download" target="_blank" rel="noreferrer">Get Tailscale (free)</a></Button>
              </div>
              {r.serving && r.url && (
                <div className="flex flex-wrap items-center gap-4">
                  <img src={`/api/remote/qr.svg?url=${encodeURIComponent(r.url)}`} alt="QR code of the phone address" className="size-36 rounded-lg bg-white p-2" />
                  <div className="text-sm">
                    <div>On your phone (Tailscale on, same account), open:</div>
                    <a className="text-primary underline" href={r.url}>{r.url}</a>
                    <div className="mt-1 text-xs text-muted-foreground">Then ⋮ → Install app. Keep the laptop running ClipForge: <code>clipforge service install</code>.</div>
                  </div>
                </div>
              )}
            </>
          )}
        </Section>

        <Section icon={Bot} title="Telegram bot (full remote control)">
          <ol className="list-decimal space-y-1 pl-5 text-sm text-muted-foreground">
            <li>In Telegram, message <a className="text-primary underline" href="https://t.me/BotFather" target="_blank" rel="noreferrer">@BotFather</a> → <code>/newbot</code> → copy the token.</li>
            <li>Paste it here → Save. Then <b>Pair</b> and send <code>/pair &lt;code&gt;</code> to your bot. Only you can use it afterwards.</li>
          </ol>
          <div className="flex flex-wrap gap-2">
            <Input type="password" className="max-w-sm" placeholder={tg.data?.token ?? '123456:ABC…'} value={token} onChange={(e) => setToken(e.target.value)} />
            <Button onClick={() => saveToken.mutate()} disabled={!token || saveToken.isPending}>Save token</Button>
            <Button variant="outline" onClick={() => pair.mutate('owner')} disabled={!tg.data?.token || isRemote()}>Pair my phone</Button>
            <Button variant="ghost" onClick={() => pair.mutate('viewer')} disabled={!tg.data?.token || isRemote()}>Add a viewer (read-only)</Button>
            {tg.data?.paired && <Button variant="outline" onClick={() => test.mutate()}>Send test</Button>}
            {tg.data?.paired && <Button variant="ghost" onClick={() => unpair.mutate()} disabled={isRemote()}>Unpair all</Button>}
          </div>
          {isRemote() && <p className="text-xs text-muted-foreground">Token, pairing and the PIN can only be changed on the laptop itself.</p>}
          {code && <div className="rounded-lg border bg-muted/30 p-3 text-sm">Send this to your bot within 10 minutes ({codeRole}): <code className="text-base font-semibold">/pair {code}</code></div>}
          <div className="text-sm">{tg.data?.paired ? '✅ Paired.' : 'Not paired yet.'} {tg.data?.running ? (tg.data?.polling ? 'Bot is listening.' : 'Bot is waiting (another computer may be running it).') : tg.data?.mode === 'worker' ? 'Worker mode: the server runs the bot.' : 'Bot loop starts with the app.'}</div>
          {(tg.data?.devices?.length ?? 0) > 0 && (
            <div className="space-y-1">
              {tg.data!.devices!.map((d) => (
                <div key={d.chat_id} className="flex flex-wrap items-center justify-between gap-2 rounded-md border px-3 py-2 text-sm">
                  <span className="min-w-0 truncate"><b>{d.name || d.kind}</b> · {d.role} · {d.kind}{d.last_seen ? ` · last used ${new Date(d.last_seen).toLocaleString()}` : ''}</span>
                  <Button size="sm" variant="ghost" onClick={() => unpairOne.mutate(d.chat_id)} disabled={isRemote()}>Unpair</Button>
                </div>
              ))}
            </div>
          )}
          <div className="space-y-2 rounded-md border p-3 text-sm">
            <div className="font-medium">PIN for dangerous actions {tg.data?.pin_required ? '(on)' : '(off)'}</div>
            <p className="text-xs text-muted-foreground">Asked in Telegram before publish now, make public, delete and resume. Stored hashed; the bot deletes the message with your PIN right away; 5 wrong tries lock it for 15 minutes.</p>
            <div className="flex flex-wrap gap-2">
              <Input type="password" inputMode="numeric" className="w-40" placeholder="4–8 digits" value={pin} onChange={(e) => setPin(e.target.value.replace(/\D/g, '').slice(0, 8))} disabled={isRemote()} />
              <Button variant="outline" onClick={() => savePin.mutate({ pin })} disabled={pin.length < 4 || isRemote()}>{tg.data?.pin_set ? 'Change PIN' : 'Set PIN'}</Button>
              {tg.data?.pin_set && <Button variant="ghost" onClick={() => savePin.mutate({})} disabled={isRemote()}>Remove PIN</Button>}
            </div>
          </div>
          <div className="flex items-center justify-between gap-3 text-sm">
            <Label htmlFor="tg-on">Bot enabled</Label>
            <Switch id="tg-on" checked={Boolean(t.enabled)} onCheckedChange={(v) => set('telegram', 'enabled', v)} />
          </div>
          <div className="grid gap-2 sm:grid-cols-2">
            {TOGGLES.map(([k, label]) => (
              <label key={k} className="flex items-center justify-between gap-3 rounded-md border px-3 py-2 text-sm">
                {label}
                <Switch checked={Boolean(t[k])} onCheckedChange={(v) => set('telegram', k, v)} />
              </label>
            ))}
          </div>
          <div className="flex items-center gap-3 text-sm">
            <Label>Daily summary at (IST)</Label>
            <Input className="w-28" value={String(t.daily_summary_time)} onChange={(e) => set('telegram', 'daily_summary_time', e.target.value)} />
          </div>
          <p className="text-xs text-muted-foreground">Send a link or a video to start a job; clips arrive as cards you can preview, approve, edit, trim and schedule. /help in the bot lists everything (docs/TELEGRAM.md).</p>
        </Section>

        <Section icon={Laptop} title="Laptop & data">
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label>Keep the laptop awake</Label>
              <Select value={String(cfg.power.keep_awake)} onValueChange={(v) => set('power', 'keep_awake', v)}>
                <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="when_busy">While a job runs or an Instagram post is due (recommended)</SelectItem>
                  <SelectItem value="always">Always while ClipForge runs</SelectItem>
                  <SelectItem value="never">Never</SelectItem>
                </SelectContent>
              </Select>
              <p className="text-xs text-muted-foreground">Closing the lid still sleeps unless Windows is set to “do nothing” (see the guide).</p>
            </div>
            <label className="flex items-center justify-between gap-3 rounded-md border px-3 py-2 text-sm">
              <span>Data saver on this device<br /><span className="text-xs text-muted-foreground">Small 540×960 previews instead of full clips</span></span>
              <Switch checked={saver} onCheckedChange={(v) => {
                setSaver(v)
                try {
                  localStorage.setItem('cf-data-saver', v ? '1' : '0')
                } catch {
                  /* ignore */
                }
              }} />
            </label>
          </div>
        </Section>

        <Section icon={Shield} title="Security">
          <div className="text-sm font-medium">Logged-in devices</div>
          {!sessions.data?.length ? <p className="text-sm text-muted-foreground">No phone has logged in yet.</p> : (
            <div className="divide-y rounded-lg border">
              {sessions.data.map((s) => (
                <div key={s.id} className="flex flex-wrap items-center gap-2 p-2 text-sm">
                  <span className="flex-1">{s.device}{s.current ? ' (this device)' : ''} · {s.ip}</span>
                  <span className="text-xs text-muted-foreground">last seen {new Date(s.last_seen + (s.last_seen.endsWith('Z') ? '' : 'Z')).toLocaleString('en-IN')}</span>
                  <Button size="sm" variant="ghost" className="text-destructive" onClick={() => revoke.mutate(s.id)}><LogOut /> Log out</Button>
                </div>
              ))}
            </div>
          )}
          <div className="pt-2 text-sm font-medium">Recent activity</div>
          <div className="max-h-56 overflow-y-auto rounded-lg border text-xs">
            {(audit.data ?? []).slice(0, 40).map((e, i) => (
              <div key={i} className="flex gap-2 border-b px-2 py-1.5 last:border-0">
                <span className="shrink-0 text-muted-foreground">{new Date(e.at + (e.at.endsWith('Z') ? '' : 'Z')).toLocaleString('en-IN')}</span>
                <span className="shrink-0 font-medium">{e.kind}</span>
                <span className="min-w-0 break-words text-muted-foreground">{e.detail}</span>
              </div>
            ))}
            {!audit.data?.length && <div className="p-2 text-muted-foreground">Nothing yet.</div>}
          </div>
        </Section>
      </CardContent>
    </Card>
  )
}
