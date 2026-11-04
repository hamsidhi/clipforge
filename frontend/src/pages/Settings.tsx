import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { KeyRound, Loader2, Plus, Save, Trash2 } from 'lucide-react'
import { toast } from 'sonner'
import { api } from '@/api/client'
import { CaptionPreview, FONT_FAMILIES } from '@/components/CaptionStylePicker'
import { ErrorBox, PageHeader } from '@/components/common'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Switch } from '@/components/ui/switch'
import PublishingSettings from '@/components/PublishingSettings'
import InstagramSettings from '@/components/InstagramSettings'
import RemoteSettings from '@/components/RemoteSettings'

type Cfg = Record<string, Record<string, unknown>>

function Row({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="grid gap-2 sm:grid-cols-[220px_1fr] sm:items-center">
      <div>
        <Label>{label}</Label>
        {hint && <p className="mt-0.5 text-xs text-muted-foreground">{hint}</p>}
      </div>
      <div>{children}</div>
    </div>
  )
}

function Sel({ value, onChange, options }: { value: string; onChange: (v: string) => void; options: [string, string][] }) {
  return (
    <Select value={value} onValueChange={onChange}>
      <SelectTrigger className="w-full sm:w-72"><SelectValue /></SelectTrigger>
      <SelectContent>
        {options.map(([v, l]) => <SelectItem key={v} value={v}>{l}</SelectItem>)}
      </SelectContent>
    </Select>
  )
}

function PresetEditor() {
  const qc = useQueryClient()
  const { data: presets = [] } = useQuery({ queryKey: ['presets'], queryFn: api.presets })
  const [p, setP] = useState({ label: '', font: 'Montserrat-ExtraBold', font_size: 80, text_color: '#FFFFFF', highlight_color: '#FF3366', keyword_color: '#00E5FF', uppercase: true })
  const save = useMutation({
    mutationFn: () => api.createPreset(p),
    onSuccess: () => {
      toast.success('Preset saved')
      setP({ ...p, label: '' })
      qc.invalidateQueries({ queryKey: ['presets'] })
    },
    onError: (e) => toast.error(e.message),
  })
  const del = useMutation({
    mutationFn: (k: string) => api.deletePreset(k),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['presets'] }),
  })
  const custom = presets.filter((x) => !x.builtin)
  return (
    <Card>
      <CardHeader>
        <CardTitle>Caption style presets</CardTitle>
        <CardDescription>4 presets ship built in. Create your own — they appear in New Job and the Clip Editor.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        {custom.length > 0 && (
          <div className="grid gap-3 sm:grid-cols-3">
            {custom.map((c) => (
              <div key={c.key} className="overflow-hidden rounded-xl border">
                <div className="grid h-16 place-items-center bg-[oklch(0.2_0.01_285)]"><CaptionPreview preset={c} /></div>
                <div className="flex items-center justify-between px-3 py-1.5 text-xs">
                  {c.label}
                  <Button variant="ghost" size="icon" className="size-7" onClick={() => del.mutate(c.key)} aria-label="Delete preset"><Trash2 /></Button>
                </div>
              </div>
            ))}
          </div>
        )}
        <div className="grid gap-6 rounded-xl border p-4 md:grid-cols-[1fr_220px]">
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-2 sm:col-span-2">
              <Label>Name</Label>
              <Input placeholder="e.g. Pink Punch" value={p.label} onChange={(e) => setP({ ...p, label: e.target.value })} />
            </div>
            <div className="space-y-2">
              <Label>Font</Label>
              <Sel value={p.font} onChange={(v) => setP({ ...p, font: v })} options={Object.keys(FONT_FAMILIES).map((f) => [f, f.replace('-', ' ')])} />
            </div>
            <div className="space-y-2">
              <Label>Size</Label>
              <Input type="number" min={50} max={110} value={p.font_size} onChange={(e) => setP({ ...p, font_size: Number(e.target.value) })} />
            </div>
            {(['text_color', 'highlight_color', 'keyword_color'] as const).map((k) => (
              <div key={k} className="space-y-2">
                <Label className="capitalize">{k.replace('_color', '').replace('text', 'base')} colour</Label>
                <input type="color" className="h-9 w-full cursor-pointer rounded border bg-transparent" value={p[k]} onChange={(e) => setP({ ...p, [k]: e.target.value })} />
              </div>
            ))}
            <div className="flex items-center gap-3 pt-6">
              <Switch checked={p.uppercase} onCheckedChange={(v) => setP({ ...p, uppercase: v })} id="upper" />
              <Label htmlFor="upper">UPPERCASE</Label>
            </div>
          </div>
          <div className="flex flex-col gap-3">
            <div className="grid flex-1 place-items-center rounded-xl bg-[radial-gradient(ellipse_at_top,oklch(0.35_0.03_250),oklch(0.16_0.01_285))] p-4">
              <CaptionPreview preset={p} size={20} />
            </div>
            <Button onClick={() => save.mutate()} disabled={!p.label.trim() || save.isPending}><Plus /> Save preset</Button>
          </div>
        </div>
      </CardContent>
    </Card>
  )
}

export default function Settings() {
  const qc = useQueryClient()
  const { data, error, isLoading } = useQuery({ queryKey: ['settings'], queryFn: api.settings })
  const ollama = useQuery({ queryKey: ['ollama-models'], queryFn: api.ollamaModels })
  const [cfg, setCfg] = useState<Cfg | null>(null)
  const [keys, setKeys] = useState<Record<string, string>>({ GEMINI_API_KEY: '', GROQ_API_KEY: '' })
  useEffect(() => {
    if (data) setCfg(structuredClone(data.config))
  }, [data])

  const save = useMutation({
    mutationFn: () => {
      const secrets: Record<string, string> = {}
      for (const [k, v] of Object.entries(keys)) if (v.trim()) secrets[k] = v.trim()
      return api.saveSettings({ config: cfg!, secrets })
    },
    onSuccess: (d) => {
      qc.setQueryData(['settings'], d)
      setKeys({ GEMINI_API_KEY: '', GROQ_API_KEY: '' })
      toast.success('Settings saved to config.yaml')
    },
    onError: (e) => toast.error(e.message),
  })
  const clearKey = useMutation({
    mutationFn: (k: string) => api.saveSettings({ secrets: { [k]: '' } }),
    onSuccess: (d) => qc.setQueryData(['settings'], d),
  })

  if (error) return <ErrorBox error={error} />
  if (isLoading || !cfg || !data) return <Skeleton className="h-96 rounded-xl" />
  const set = (section: string, key: string, value: unknown) => setCfg((c) => ({ ...c!, [section]: { ...c![section], [key]: value } }))
  const v = (section: string, key: string) => String(cfg[section][key])

  return (
    <>
      <PageHeader
        title="Settings"
        description="Saved to config.yaml (API keys go to .env and are never shown in full)."
        actions={<Button onClick={() => save.mutate()} disabled={save.isPending}>{save.isPending ? <Loader2 className="animate-spin" /> : <Save />} Save settings</Button>}
      />
      <div className="space-y-6">
        <Card>
          <CardHeader><CardTitle>AI (clip selection)</CardTitle></CardHeader>
          <CardContent className="space-y-5">
            <Row label="Provider" hint="Ollama runs offline on this computer.">
              <Sel value={v('llm', 'provider')} onChange={(x) => set('llm', 'provider', x)}
                options={[['ollama', 'Ollama (local)'], ['gemini', 'Gemini (free tier)'], ['groq', 'Groq (free tier)'], ['mock', 'Mock (testing)']]} />
            </Row>
            <Row label="Model" hint={v('llm', 'provider') === 'ollama' ? (ollama.data?.running ? 'From `ollama list`' : 'Ollama is not running') : 'auto = provider default'}>
              {v('llm', 'provider') === 'ollama' ? (
                <Sel value={v('llm', 'model')} onChange={(x) => set('llm', 'model', x)}
                  options={[['auto', `Auto${ollama.data?.recommended ? ` (${ollama.data.recommended})` : ''}`], ...(ollama.data?.models ?? []).map((m) => [m, m] as [string, string])]} />
              ) : (
                <Input className="sm:w-72" value={v('llm', 'model')} onChange={(e) => set('llm', 'model', e.target.value)} />
              )}
            </Row>
            {(['GEMINI_API_KEY', 'GROQ_API_KEY'] as const).map((k) => (
              <Row key={k} label={k === 'GEMINI_API_KEY' ? 'Gemini API key' : 'Groq API key'}
                hint={k === 'GEMINI_API_KEY' ? 'Free: aistudio.google.com/apikey' : 'Free: console.groq.com/keys'}>
                <div className="flex flex-wrap items-center gap-2">
                  <div className="relative sm:w-72">
                    <KeyRound className="absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" />
                    <Input type="password" className="pl-9" placeholder={data.secrets[k] ?? 'not set'} value={keys[k]} onChange={(e) => setKeys({ ...keys, [k]: e.target.value })} />
                  </div>
                  {data.secrets[k] && <Button variant="ghost" size="sm" onClick={() => clearKey.mutate(k)}>Remove</Button>}
                </div>
              </Row>
            ))}
          </CardContent>
        </Card>

        <Card>
          <CardHeader><CardTitle>Transcription</CardTitle></CardHeader>
          <CardContent className="space-y-5">
            <Row label="Whisper model" hint="auto = large-v3 on NVIDIA GPU, large-v3-turbo on CPU">
              <Sel value={v('whisper', 'model')} onChange={(x) => set('whisper', 'model', x)}
                options={[['auto', 'Auto'], ['small', 'small (fast, poor Hindi)'], ['medium', 'medium'], ['large-v3-turbo', 'large-v3-turbo (best on CPU)'], ['large-v3', 'large-v3 (GPU)']]} />
            </Row>
            <Row label="Language">
              <Sel value={v('whisper', 'language')} onChange={(x) => set('whisper', 'language', x)} options={[['hi', 'Hindi / Hinglish'], ['en', 'English'], ['auto', 'Auto-detect']]} />
            </Row>
          </CardContent>
        </Card>

        <Card>
          <CardHeader><CardTitle>Clips &amp; captions defaults</CardTitle></CardHeader>
          <CardContent className="space-y-5">
            <Row label="Clips per video"><Input type="number" min={1} max={50} className="w-28" value={v('clips', 'count')} onChange={(e) => set('clips', 'count', Number(e.target.value))} /></Row>
            <Row label="Length (s)">
              <div className="flex items-center gap-2">
                <Input type="number" className="w-24" value={v('clips', 'min_sec')} onChange={(e) => set('clips', 'min_sec', Number(e.target.value))} />–
                <Input type="number" className="w-24" value={v('clips', 'max_sec')} onChange={(e) => set('clips', 'max_sec', Number(e.target.value))} />
              </div>
            </Row>
            <Row label="Caption script"><Sel value={v('captions', 'script')} onChange={(x) => set('captions', 'script', x)} options={[['roman', 'Roman Hinglish'], ['devanagari', 'Devanagari']]} /></Row>
            <Row label="Hook title overlay">
              <Switch checked={Boolean(cfg.captions.hook_title)} onCheckedChange={(x) => set('captions', 'hook_title', x)} />
            </Row>
            <Row label="Layout"><Sel value={v('reframe', 'layout')} onChange={(x) => set('reframe', 'layout', x)} options={[['auto', 'Auto'], ['track', 'Track'], ['split', 'Split'], ['blur', 'Blur']]} /></Row>
          </CardContent>
        </Card>

        <Card>
          <CardHeader><CardTitle>Render quality &amp; output</CardTitle></CardHeader>
          <CardContent className="space-y-5">
            <Row label="Encoder preset" hint="slower = smaller/better files">
              <Sel value={v('render', 'preset')} onChange={(x) => set('render', 'preset', x)} options={[['veryfast', 'veryfast (default)'], ['fast', 'fast'], ['medium', 'medium (best quality)']]} />
            </Row>
            <Row label="Quality (CRF)" hint="lower = better quality, 18–23 is good">
              <Input type="number" min={14} max={30} className="w-24" value={v('render', 'crf')} onChange={(e) => set('render', 'crf', Number(e.target.value))} />
            </Row>
            <Row label="Output folder" hint="Relative paths are inside the ClipForge folder. Restart after changing.">
              <Input className="sm:w-96" value={v('output', 'dir')} onChange={(e) => set('output', 'dir', e.target.value)} />
            </Row>
          </CardContent>
        </Card>

        <PublishingSettings cfg={cfg} set={set} />
        <InstagramSettings cfg={cfg} set={set} />
        <RemoteSettings cfg={cfg} set={set} />

        <PresetEditor />
      </div>
    </>
  )
}
