import { useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Clapperboard, KeyRound, Loader2, LogIn } from 'lucide-react'
import { request } from '@/api/client'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

interface AuthStatus { authenticated: boolean; local: boolean; password_set: boolean; setup_required: boolean; https: boolean }

/** Login screen for the phone (Tailscale) — the laptop itself (localhost) never needs it. */
export default function Login() {
  const [params] = useSearchParams()
  const next = params.get('next') || '/'
  const [password, setPassword] = useState('')
  const [remember, setRemember] = useState(true)
  const { data: st } = useQuery({ queryKey: ['auth'], queryFn: () => request<AuthStatus>('/api/auth/status') })
  const login = useMutation({
    mutationFn: () => request<{ ok: boolean }>('/api/auth/login', { method: 'POST', body: JSON.stringify({ password, remember }) }),
    onSuccess: () => window.location.assign(next.startsWith('/') && !next.startsWith('//') ? next : '/'),
  })
  if (st?.authenticated) {
    window.location.replace(next.startsWith('/') && !next.startsWith('//') ? next : '/')
    return null
  }
  return (
    <div className="grid min-h-dvh place-items-center bg-background p-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <div className="mb-2 grid size-10 place-items-center rounded-xl bg-primary text-primary-foreground"><Clapperboard className="size-5" /></div>
          <CardTitle>ClipForge</CardTitle>
          <CardDescription>
            {st?.setup_required
              ? 'No password yet. On the laptop, run `clipforge auth set-password` (or Settings → Phone & remote), then come back.'
              : 'Log in to control ClipForge on your laptop.'}
          </CardDescription>
        </CardHeader>
        {!st?.setup_required && (
          <CardContent>
            <form className="space-y-4" onSubmit={(e) => { e.preventDefault(); login.mutate() }}>
              <div className="space-y-1.5">
                <Label htmlFor="pw">Password</Label>
                <Input id="pw" type="password" autoComplete="current-password" autoFocus value={password}
                  onChange={(e) => setPassword(e.target.value)} className="h-11" />
              </div>
              <label className="flex items-center gap-2 text-sm">
                <Checkbox checked={remember} onCheckedChange={(v) => setRemember(v === true)} /> Remember this device for 30 days
              </label>
              {login.error && <p className="text-sm text-destructive">{(login.error as Error).message}</p>}
              <Button type="submit" className="h-11 w-full" disabled={!password || login.isPending}>
                {login.isPending ? <Loader2 className="animate-spin" /> : <LogIn />} Log in
              </Button>
              {st && !st.https && (
                <p className="flex gap-2 text-xs text-amber-300"><KeyRound className="size-4 shrink-0" />
                  This connection isn't HTTPS. Use the Tailscale address (https://…ts.net) — see `clipforge remote setup`.</p>
              )}
            </form>
          </CardContent>
        )}
      </Card>
    </div>
  )
}
