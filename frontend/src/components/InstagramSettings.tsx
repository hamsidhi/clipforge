import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Camera, CheckCircle2, ExternalLink, Save } from 'lucide-react'
import { toast } from 'sonner'
import { pub, useMetaApp } from '@/api/publish'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'

type Cfg = Record<string, Record<string, unknown>>

/** Settings → Instagram: Meta app (id + secret in .env, masked) and posting behaviour. */
export default function InstagramSettings({ cfg, set }: { cfg: Cfg; set: (section: string, key: string, value: unknown) => void }) {
  const qc = useQueryClient()
  const { data: app } = useMetaApp()
  const [appId, setAppId] = useState('')
  const [secret, setSecret] = useState('')
  const [configId, setConfigId] = useState('')
  const save = useMutation({
    mutationFn: () => pub.saveMetaApp({
      ...(appId ? { app_id: appId } : {}), ...(secret ? { app_secret: secret } : {}), ...(configId ? { login_config_id: configId } : {}),
    }),
    onSuccess: () => {
      toast.success('Meta app saved')
      setSecret('')
      qc.invalidateQueries({ queryKey: ['meta-app'] })
    },
    onError: (e) => toast.error(e.message),
  })
  const i = cfg.instagram
  return (
    <Card id="instagram">
      <CardHeader>
        <CardTitle className="flex items-center gap-2"><Camera className="size-5 text-pink-400" /> Instagram Reels</CardTitle>
        <CardDescription>
          Post Reels automatically through the official Instagram API (free). Per-account times are on the Accounts page.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        <div className="rounded-xl border p-4">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <div className="font-medium">Meta app (one time, ~15 minutes)</div>
            {app?.configured ? <span className="flex items-center gap-1 text-sm text-success"><CheckCircle2 className="size-4" /> set up</span>
              : <span className="text-sm text-amber-300">not set up</span>}
          </div>
          <ol className="list-decimal space-y-1 pl-5 text-sm text-muted-foreground">
            <li>Instagram app → Settings → <b>Account type and tools</b> → switch to a <b>Professional</b> account (Creator or Business). Free.</li>
            <li>Link it to a <b>Facebook Page</b> (create one if needed): Instagram → Accounts Center, or the Page's settings → Linked accounts.</li>
            <li>
              <a className="text-primary underline" href="https://developers.facebook.com/apps/creation/" target="_blank" rel="noreferrer">Create a Meta app</a>{' '}
              (type <b>Business</b>), add the products <b>Facebook Login for Business</b> and <b>Instagram</b>.
            </li>
            <li>Facebook Login → Settings → <b>Valid OAuth Redirect URIs</b>: <code>{app?.redirect_uri ?? 'http://localhost:8000/api/instagram/oauth/callback'}</code></li>
            <li>App settings → Basic: copy the <b>App ID</b> and <b>App Secret</b> here. The app can stay in <b>Development</b> mode —
              no App Review is needed for your own accounts (your Facebook user is the app's admin).</li>
            <li>Then Accounts → Instagram → <b>Add Instagram account</b> (on this laptop).</li>
          </ol>
          <div className="mt-4 grid gap-3 md:grid-cols-3">
            <div className="space-y-1.5">
              <Label>App ID</Label>
              <Input placeholder={app?.app_id || '1234567890'} value={appId} onChange={(e) => setAppId(e.target.value)} />
            </div>
            <div className="space-y-1.5">
              <Label>App Secret</Label>
              <Input type="password" placeholder={app?.app_secret ?? 'paste the secret'} value={secret} onChange={(e) => setSecret(e.target.value)} />
            </div>
            <div className="space-y-1.5">
              <Label>Login configuration ID (optional)</Label>
              <Input placeholder={app?.login_config_id || 'Facebook Login for Business only'} value={configId} onChange={(e) => setConfigId(e.target.value)} />
            </div>
          </div>
          <div className="mt-3 flex flex-wrap gap-2">
            <Button onClick={() => save.mutate()} disabled={(!appId && !secret && !configId) || save.isPending}><Save /> Save Meta app</Button>
            <Button variant="ghost" asChild><a href="https://developers.facebook.com/apps/" target="_blank" rel="noreferrer"><ExternalLink /> My Meta apps</a></Button>
          </div>
          <p className="mt-2 text-xs text-muted-foreground">The secret is stored in <code>.env</code> on this computer and never shown again.</p>
        </div>

        <div className="grid gap-5 md:grid-cols-4">
          <div className="space-y-2">
            <Label>Mode</Label>
            <Select value={String(i.mode)} onValueChange={(v) => set('instagram', 'mode', v)}>
              <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="api">API — ClipForge posts at the slot</SelectItem>
                <SelectItem value="assisted">Assisted — Instagram kit, you post</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-2">
            <Label>Upload this early (min)</Label>
            <Input type="number" min={5} max={600} value={String(i.upload_lead_minutes)} onChange={(e) => set('instagram', 'upload_lead_minutes', Number(e.target.value))} />
          </div>
          <div className="space-y-2">
            <Label>Give up after (min late)</Label>
            <Input type="number" min={0} max={1440} value={String(i.max_late_minutes)} onChange={(e) => set('instagram', 'max_late_minutes', Number(e.target.value))} />
          </div>
          <div className="space-y-2">
            <Label>Default times (IST)</Label>
            <Input defaultValue={(i.slots as string[]).join(', ')} onBlur={(e) => set('instagram', 'slots', e.target.value.split(/[,\s]+/).filter(Boolean))} />
          </div>
        </div>
        <p className="text-xs text-muted-foreground">
          Instagram's API can't schedule: ClipForge uploads the Reel {String(i.upload_lead_minutes)} min before the slot and publishes at the slot,
          so this laptop must be on with ClipForge running then (keep the app open, or <code>clipforge schedule-task install --daemon</code>).
          A slot missed by more than {String(i.max_late_minutes)} min is never posted late — it shows “Missed slot” with Reschedule.
        </p>
      </CardContent>
    </Card>
  )
}
