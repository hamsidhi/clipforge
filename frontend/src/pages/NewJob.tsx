import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Clock, FileVideo, Link2, Loader2, MonitorPlay, Sparkles, Upload, X } from 'lucide-react'
import { toast } from 'sonner'
import { api, uploadFile } from '@/api/client'
import { formatDuration } from '@/api/hooks'
import type { JobSettings, Layout, Provider } from '@/api/types'
import CaptionStylePicker from '@/components/CaptionStylePicker'
import { PageHeader } from '@/components/common'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Progress } from '@/components/ui/progress'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Slider } from '@/components/ui/slider'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { cn } from '@/lib/utils'

const LAYOUTS: { value: Layout; label: string; hint: string }[] = [
  { value: 'auto', label: 'Auto', hint: 'Track the speaker; split-screen on wide 2-person shots' },
  { value: 'track', label: 'Track', hint: 'Always follow the biggest face' },
  { value: 'split', label: 'Split', hint: 'Stack both people top/bottom when two are visible' },
  { value: 'blur', label: 'Blur', hint: 'Full 16:9 frame over a blurred background' },
]

function parseTime(s: string): number | null {
  if (!s.trim()) return null
  const parts = s.trim().split(':').map(Number)
  if (parts.some((p) => isNaN(p))) return null
  return parts.reduce((acc, p) => acc * 60 + p, 0)
}

export default function NewJob() {
  const nav = useNavigate()
  const [shared] = useSearchParams()
  const [url, setUrl] = useState(() => shared.get('url') ?? '')
  const [file, setFile] = useState<File | null>(null)
  const [fileDuration, setFileDuration] = useState<number | null>(null)
  const [dragging, setDragging] = useState(false)
  const [uploadPct, setUploadPct] = useState<number | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  const [clips, setClips] = useState(20)
  const [minSec, setMinSec] = useState(20)
  const [maxSec, setMaxSec] = useState(59)
  const [script, setScript] = useState<'roman' | 'devanagari'>('roman')
  const [preset, setPreset] = useState('bold-yellow-pop')
  const [layout, setLayout] = useState<Layout>('auto')
  const [provider, setProvider] = useState<Provider>('ollama')
  const [model, setModel] = useState('auto')
  const [secStart, setSecStart] = useState('')
  const [secEnd, setSecEnd] = useState('')

  const settings = useQuery({ queryKey: ['settings'], queryFn: api.settings })
  const ollama = useQuery({ queryKey: ['ollama-models'], queryFn: api.ollamaModels, staleTime: 30_000 })
  useEffect(() => {
    const cfg = settings.data?.config
    if (!cfg) return
    setClips(Number(cfg.clips.count))
    setMinSec(Number(cfg.clips.min_sec))
    setMaxSec(Number(cfg.clips.max_sec))
    setScript(cfg.captions.script as 'roman' | 'devanagari')
    setPreset(String(cfg.captions.preset))
    setLayout(cfg.reframe.layout as Layout)
    setProvider(cfg.llm.provider as Provider)
    setModel(String(cfg.llm.model))
  }, [settings.data])

  const isUrl = /^https?:\/\//.test(url.trim())
  const probe = useQuery({
    queryKey: ['probe', url.trim()],
    queryFn: () => api.probe(url.trim()),
    enabled: isUrl && !file,
    retry: false,
    staleTime: Infinity,
  })

  const section = useMemo(() => {
    const a = parseTime(secStart)
    const b = parseTime(secEnd)
    return a != null && b != null && b > a ? ([a, b] as [number, number]) : null
  }, [secStart, secEnd])
  const duration = section ? section[1] - section[0] : file ? fileDuration : probe.data?.duration ?? null
  const estimate = useQuery({
    queryKey: ['estimate', duration, clips, provider],
    queryFn: () => api.estimate(duration!, clips, provider),
    enabled: !!duration,
  })

  const pickFile = (f: File | undefined) => {
    if (!f) return
    if (!f.type.startsWith('video/') && !/\.(mp4|mkv|mov|webm|m4v|avi)$/i.test(f.name)) {
      toast.error('That does not look like a video file')
      return
    }
    setFile(f)
    setUrl('')
    setFileDuration(null)
    const v = document.createElement('video')
    v.preload = 'metadata'
    v.onloadedmetadata = () => {
      setFileDuration(v.duration)
      URL.revokeObjectURL(v.src)
    }
    v.src = URL.createObjectURL(f)
  }

  const create = useMutation({
    mutationFn: async () => {
      const s: JobSettings = {
        clips, min_sec: minSec, max_sec: maxSec, caption_script: preset === 'hindi-devanagari' ? 'devanagari' : script,
        caption_preset: preset, layout, provider, model: provider === 'ollama' ? model : 'auto',
      }
      if (section) s.section = section
      if (file) {
        setUploadPct(0)
        const upload_id = await uploadFile(file, setUploadPct)
        return api.createJob({ upload_id, settings: s })
      }
      return api.createJob({ url: url.trim(), settings: s })
    },
    onSuccess: (job) => {
      toast.success('Job started')
      nav(`/jobs/${job.id}`)
    },
    onError: (e) => {
      setUploadPct(null)
      toast.error(e.message)
    },
  })

  const canSubmit = (isUrl || !!file) && !create.isPending && minSec < maxSec

  return (
    <>
      <PageHeader title="New clip job" description="Paste a YouTube link or drop a video. Everything runs on this computer." />
      <div className="grid grid-cols-[minmax(0,1fr)] gap-6 lg:grid-cols-[minmax(0,1fr)_340px]">
        <div className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle>Source video</CardTitle>
              <CardDescription>A full podcast episode works best (1–3 hours, 2+ people).</CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="relative">
                <Link2 className="absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" />
                <Input
                  className="h-11 pl-9"
                  placeholder="https://www.youtube.com/watch?v=…"
                  value={url}
                  disabled={!!file}
                  onChange={(e) => setUrl(e.target.value)}
                />
              </div>
              {isUrl && !file && (
                <div className="flex items-center gap-3 rounded-lg bg-muted/50 p-3 text-sm">
                  {probe.isLoading ? (
                    <>
                      <Loader2 className="size-4 animate-spin" /> Checking link…
                    </>
                  ) : probe.data ? (
                    <>
                      {probe.data.thumbnail ? (
                        <img src={probe.data.thumbnail} alt="" className="h-12 w-20 rounded object-cover" />
                      ) : (
                        <MonitorPlay className="size-5" />
                      )}
                      <div className="min-w-0">
                        <div className="truncate font-medium">{probe.data.title}</div>
                        <div className="text-xs text-muted-foreground">
                          {probe.data.channel} · {formatDuration(probe.data.duration)}
                          {probe.data.chapters ? ` · ${probe.data.chapters} chapters` : ''}
                        </div>
                      </div>
                    </>
                  ) : (
                    <span className="text-muted-foreground">
                      {probe.error ? `Couldn't preview this link (${probe.error.message}). You can still start the job.` : ''}
                    </span>
                  )}
                </div>
              )}
              <div className="flex items-center gap-3 text-xs text-muted-foreground">
                <div className="h-px flex-1 bg-border" /> or <div className="h-px flex-1 bg-border" />
              </div>
              <div
                onDragOver={(e) => {
                  e.preventDefault()
                  setDragging(true)
                }}
                onDragLeave={() => setDragging(false)}
                onDrop={(e) => {
                  e.preventDefault()
                  setDragging(false)
                  pickFile(e.dataTransfer.files[0])
                }}
                onClick={() => !file && inputRef.current?.click()}
                className={cn(
                  'flex cursor-pointer flex-col items-center justify-center gap-2 rounded-xl border border-dashed px-4 py-8 text-center transition-colors',
                  dragging ? 'border-primary bg-primary/5' : 'hover:border-primary/50',
                  file && 'cursor-default',
                )}
              >
                <input ref={inputRef} type="file" accept="video/*,.mkv" className="hidden" onChange={(e) => pickFile(e.target.files?.[0])} />
                {file ? (
                  <div className="flex w-full items-center gap-3 text-left">
                    <FileVideo className="size-8 shrink-0 text-primary" />
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-sm font-medium">{file.name}</div>
                      <div className="text-xs text-muted-foreground">
                        {(file.size / 1e9).toFixed(2)} GB {fileDuration ? `· ${formatDuration(fileDuration)}` : ''}
                      </div>
                      {uploadPct != null && <Progress value={uploadPct * 100} className="mt-2 h-1.5" />}
                    </div>
                    <Button variant="ghost" size="icon" onClick={(e) => { e.stopPropagation(); setFile(null) }} aria-label="Remove file">
                      <X />
                    </Button>
                  </div>
                ) : (
                  <>
                    <Upload className="size-6 text-muted-foreground" />
                    <div className="text-sm">
                      <span className="font-medium text-foreground">Drop a video file</span> <span className="text-muted-foreground">or click to browse</span>
                    </div>
                    <div className="text-xs text-muted-foreground">mp4, mkv, mov, webm — large files are fine</div>
                  </>
                )}
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Clips</CardTitle>
            </CardHeader>
            <CardContent className="space-y-6">
              <div className="space-y-3">
                <div className="flex justify-between text-sm">
                  <Label>Number of clips</Label>
                  <span className="font-semibold tabular-nums">{clips}</span>
                </div>
                <Slider min={15} max={25} step={1} value={[clips]} onValueChange={([v]) => setClips(v)} />
              </div>
              <div className="grid grid-cols-2 gap-4">
                <div className="space-y-2">
                  <Label htmlFor="min">Min length (s)</Label>
                  <Input id="min" type="number" min={5} max={120} value={minSec} onChange={(e) => setMinSec(Number(e.target.value))} />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="max">Max length (s)</Label>
                  <Input id="max" type="number" min={10} max={180} value={maxSec} onChange={(e) => setMaxSec(Number(e.target.value))} />
                </div>
              </div>
              {minSec >= maxSec && <p className="text-xs text-destructive">Min length must be smaller than max length.</p>}
              <div className="space-y-2">
                <Label>Only part of the video (optional)</Label>
                <div className="flex items-center gap-2">
                  <Input placeholder="start e.g. 10:00" value={secStart} onChange={(e) => setSecStart(e.target.value)} />
                  <span className="text-muted-foreground">–</span>
                  <Input placeholder="end e.g. 40:00" value={secEnd} onChange={(e) => setSecEnd(e.target.value)} />
                </div>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Captions</CardTitle>
              <CardDescription>Burned in, word-by-word highlight. No watermark.</CardDescription>
            </CardHeader>
            <CardContent className="space-y-5">
              <ToggleGroup
                type="single"
                variant="outline"
                value={preset === 'hindi-devanagari' ? 'devanagari' : script}
                onValueChange={(v) => v && setScript(v as 'roman' | 'devanagari')}
              >
                <ToggleGroupItem value="roman" className="px-4">Roman Hinglish</ToggleGroupItem>
                <ToggleGroupItem value="devanagari" className="px-4">देवनागरी</ToggleGroupItem>
              </ToggleGroup>
              <CaptionStylePicker value={preset} onChange={setPreset} />
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Framing &amp; AI</CardTitle>
            </CardHeader>
            <CardContent className="grid gap-5 sm:grid-cols-2">
              <div className="space-y-2 sm:col-span-2">
                <Label>Layout</Label>
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                  {LAYOUTS.map((l) => (
                    <button
                      type="button"
                      key={l.value}
                      onClick={() => setLayout(l.value)}
                      className={cn('rounded-lg border p-3 text-left text-sm transition-colors hover:border-primary/50',
                        layout === l.value && 'border-primary bg-primary/5')}
                    >
                      <div className="font-medium">{l.label}</div>
                      <div className="mt-1 text-xs leading-snug text-muted-foreground">{l.hint}</div>
                    </button>
                  ))}
                </div>
              </div>
              <div className="space-y-2">
                <Label>LLM provider</Label>
                <Select value={provider} onValueChange={(v) => setProvider(v as Provider)}>
                  <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="ollama">Ollama (local, offline)</SelectItem>
                    <SelectItem value="gemini" disabled={!settings.data?.secrets.GEMINI_API_KEY}>Gemini (free tier)</SelectItem>
                    <SelectItem value="groq" disabled={!settings.data?.secrets.GROQ_API_KEY}>Groq (free tier)</SelectItem>
                    <SelectItem value="mock">Mock (testing only)</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              {provider === 'ollama' && (
                <div className="space-y-2">
                  <Label>Model</Label>
                  <Select value={model} onValueChange={setModel}>
                    <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="auto">Auto {ollama.data?.recommended ? `(${ollama.data.recommended})` : ''}</SelectItem>
                      {ollama.data?.models.map((m) => (
                        <SelectItem key={m} value={m}>{m}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  {ollama.data && !ollama.data.running && (
                    <p className="text-xs text-destructive">Ollama is not running — start the Ollama app first.</p>
                  )}
                </div>
              )}
            </CardContent>
          </Card>
        </div>

        <div className="lg:sticky lg:top-8 lg:self-start">
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2"><Clock className="size-4" /> Estimate</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4 text-sm">
              {estimate.data ? (
                <>
                  <div className="text-3xl font-semibold tabular-nums">~{formatDuration(estimate.data.total_sec)}</div>
                  <div className="space-y-1.5">
                    {Object.entries(estimate.data.parts).map(([k, v]) => (
                      <div key={k} className="flex justify-between text-muted-foreground">
                        <span className="capitalize">{k}</span>
                        <span className="tabular-nums">{formatDuration(v)}</span>
                      </div>
                    ))}
                  </div>
                  <p className="text-xs text-muted-foreground">Rough guess for this computer. You can close the tab — the job keeps running.</p>
                </>
              ) : (
                <p className="text-muted-foreground">Add a video to see how long it will take.</p>
              )}
              <Button className="w-full" size="lg" disabled={!canSubmit} onClick={() => create.mutate()}>
                {create.isPending ? <Loader2 className="animate-spin" /> : <Sparkles />}
                {uploadPct != null && create.isPending ? `Uploading ${Math.round(uploadPct * 100)}%` : 'Create clips'}
              </Button>
            </CardContent>
          </Card>
        </div>
      </div>
    </>
  )
}
