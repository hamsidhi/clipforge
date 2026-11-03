import { useRef } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { CheckCircle2, ExternalLink, FileKey, Upload } from 'lucide-react'
import { toast } from 'sonner'
import { pub, usePublishing } from '@/api/publish'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'

type Cfg = Record<string, Record<string, unknown>>

export default function PublishingSettings({ cfg, set }: { cfg: Cfg; set: (section: string, key: string, value: unknown) => void }) {
  const qc = useQueryClient()
  const { data: status } = usePublishing()
  const fileRef = useRef<HTMLInputElement>(null)
  const upload = useMutation({
    mutationFn: (f: File) => pub.uploadClientSecret(f),
    onSuccess: (r) => {
      toast.success(`Saved (${r.type === 'installed' ? 'Desktop app' : 'Web'} client ${r.client_id})`)
      qc.invalidateQueries({ queryKey: ['publishing'] })
    },
    onError: (e) => toast.error(e.message),
  })
  const p = cfg.publishing
  return (
    <Card id="publishing">
      <CardHeader>
        <CardTitle>Publishing (YouTube)</CardTitle>
        <CardDescription>How Shorts reach your channels. Per-channel times and templates are on the Channels page.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        <div className="grid gap-5 md:grid-cols-2">
          <div className="space-y-2">
            <Label>Mode</Label>
            <Select value={String(p.mode)} onValueChange={(v) => set('publishing', 'mode', v)}>
              <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="assisted">Assisted — ClipForge prepares, you upload in YouTube Studio</SelectItem>
                <SelectItem value="api">API — ClipForge uploads (private + scheduled)</SelectItem>
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground">
              API mode uploads ahead of time with a publish date, so YouTube publishes on time even if this laptop is off.
            </p>
          </div>
          <div className="space-y-3">
            <div className="flex items-center justify-between gap-3 text-sm">
              <Label htmlFor="aud">My API project passed Google's audit</Label>
              <Switch id="aud" checked={Boolean(p.api_audited)} onCheckedChange={(v) => set('publishing', 'api_audited', v)} />
            </div>
            <div className="flex items-center justify-between gap-3 text-sm">
              <Label htmlFor="lock">Unaudited uploads get locked to private</Label>
              <Switch id="lock" checked={Boolean(p.unverified_lock)} onCheckedChange={(v) => set('publishing', 'unverified_lock', v)} />
            </div>
            <p className="text-xs text-muted-foreground">
              Google lifted that lock on 2 Nov 2026. Turn this on only if your API uploads come back "Locked private" — then API
              mode does a single test upload and you use assisted mode until audited.{' '}
              {status && <a className="text-primary underline" href={status.audit_form} target="_blank" rel="noreferrer">Audit &amp; quota form</a>}
            </p>
          </div>
        </div>

        <div className="rounded-xl border p-4">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2 font-medium"><FileKey className="size-4" /> Google OAuth client (client_secret.json)</div>
            {status?.client_secret ? (
              <span className="flex items-center gap-1 text-sm text-success"><CheckCircle2 className="size-4" /> installed</span>
            ) : <span className="text-sm text-amber-300">missing</span>}
          </div>
          <ol className="list-decimal space-y-1 pl-5 text-sm text-muted-foreground">
            <li><a className="text-primary underline" href="https://console.cloud.google.com/projectcreate" target="_blank" rel="noreferrer">Create a Google Cloud project</a> (free).</li>
            <li>APIs &amp; Services → Library → enable <b>YouTube Data API v3</b> and <b>YouTube Analytics API</b>.</li>
            <li>OAuth consent screen → External → add your Google account as a test user. Tip: set publishing status to <b>In production</b> (unverified is fine for personal use) so the login doesn't expire every 7 days.</li>
            <li>Credentials → Create credentials → OAuth client ID → <b>Desktop app</b> → Download JSON.</li>
            <li>Upload that file here, then go to Channels → Add channel.</li>
          </ol>
          <input ref={fileRef} type="file" accept=".json,application/json" className="hidden"
            onChange={(e) => e.target.files?.[0] && upload.mutate(e.target.files[0])} />
          <div className="mt-3 flex flex-wrap gap-2">
            <Button variant="outline" onClick={() => fileRef.current?.click()} disabled={upload.isPending}><Upload /> Upload client_secret.json</Button>
            <Button variant="ghost" asChild><a href="https://console.cloud.google.com/apis/credentials" target="_blank" rel="noreferrer"><ExternalLink /> Google Cloud credentials</a></Button>
          </div>
        </div>

        <div className="grid gap-5 md:grid-cols-4">
          <div className="space-y-2">
            <Label>Default slots (IST)</Label>
            <Input defaultValue={(p.slots as string[]).join(', ')} onBlur={(e) => set('publishing', 'slots', e.target.value.split(/[,\s]+/).filter(Boolean))} />
          </div>
          <div className="space-y-2">
            <Label>Posts / day</Label>
            <Input type="number" min={1} max={20} value={String(p.posts_per_day)} onChange={(e) => set('publishing', 'posts_per_day', Number(e.target.value))} />
          </div>
          <div className="space-y-2">
            <Label>Upload ahead (days)</Label>
            <Input type="number" min={1} max={14} value={String(p.upload_ahead_days)} onChange={(e) => set('publishing', 'upload_ahead_days', Number(e.target.value))} />
          </div>
          <div className="space-y-2">
            <Label>Uploads left today</Label>
            <div className="pt-2 text-sm tabular-nums">{status ? `${status.quota.inserts}/${status.quota.inserts_limit} · ${status.quota.units.toLocaleString()} units` : '—'}</div>
          </div>
        </div>
      </CardContent>
    </Card>
  )
}
