import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, Check, ChevronLeft, ChevronRight, Loader2, RefreshCw, Save, Send, Undo2, X } from 'lucide-react'
import { useIsMobile } from '@/lib/useIsMobile'
import { toast } from 'sonner'
import { api, videoSrc } from '@/api/client'
import { useClips, useJobEvents } from '@/api/hooks'
import type { CaptionStyle, ClipDetail, CropOverride as Override, Layout } from '@/api/types'
import CaptionStylePicker from '@/components/CaptionStylePicker'
import { ErrorBox, ScoreBadge } from '@/components/common'
import CropOverride from '@/components/CropOverride'
import PhoneFramePlayer from '@/components/PhoneFramePlayer'
import WaveformTrimmer from '@/components/WaveformTrimmer'
import AddToQueueDialog from '@/components/AddToQueueDialog'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Progress } from '@/components/ui/progress'
import { Skeleton } from '@/components/ui/skeleton'
import { Slider } from '@/components/ui/slider'
import { Switch } from '@/components/ui/switch'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Textarea } from '@/components/ui/textarea'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { cn } from '@/lib/utils'

interface Draft {
  start: number
  end: number
  title: string
  hook: string
  word_edits: Record<string, string>
  caption_style: CaptionStyle
  layout: Layout | null
  crop_overrides: Override[]
}

const draftOf = (c: ClipDetail): Draft => ({
  start: c.start, end: c.end, title: c.title, hook: c.hook, word_edits: { ...c.word_edits },
  caption_style: { ...c.caption_style }, layout: c.layout, crop_overrides: [...c.crop_overrides],
})

function WordEditor({ clip, edits, onChange }: { clip: ClipDetail; edits: Record<string, string>; onChange: (e: Record<string, string>) => void }) {
  const [editing, setEditing] = useState<number | null>(null)
  const [val, setVal] = useState('')
  const commit = (i: number, original: string) => {
    const next = { ...edits }
    if (val === original) delete next[String(i)]
    else next[String(i)] = val
    onChange(next)
    setEditing(null)
  }
  return (
    <div className="flex flex-wrap gap-1.5 rounded-lg border bg-muted/20 p-3 leading-none">
      {clip.words.map((w) => {
        const edited = edits[String(w.index)]
        const shown = edited ?? w.text
        if (editing === w.index) {
          return (
            <input
              key={w.index}
              autoFocus
              value={val}
              onChange={(e) => setVal(e.target.value)}
              onBlur={() => commit(w.index, w.text)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') commit(w.index, w.text)
                if (e.key === 'Escape') setEditing(null)
              }}
              className="h-7 rounded-md border border-primary bg-background px-2 text-sm outline-none"
              style={{ width: `${Math.max(3, val.length + 2)}ch` }}
            />
          )
        }
        return (
          <button
            type="button"
            key={w.index}
            title={`${w.start.toFixed(2)}s — click to edit`}
            onClick={() => {
              setEditing(w.index)
              setVal(shown)
            }}
            className={cn(
              'h-7 rounded-md px-2 text-sm transition-colors hover:bg-accent',
              edited !== undefined && 'bg-primary/15 text-primary',
              edited === '' && 'line-through opacity-60',
            )}
          >
            {shown || w.text}
          </button>
        )
      })}
    </div>
  )
}

/** Remount per clip: Prev/Next must not carry the draft, "rendering" state or word editor over. */
export default function ClipEditorPage() {
  const clipId = useParams().clipId!
  return <ClipEditor key={clipId} clipId={clipId} />
}

function ClipEditor({ clipId }: { clipId: string }) {
  const jobId = Number(clipId.split('-')[0])
  const nav = useNavigate()
  const qc = useQueryClient()
  const { data: clip, error, refetch } = useQuery({ queryKey: ['clip', clipId], queryFn: () => api.clip(clipId) })
  const { data: allClips = [] } = useClips(jobId)
  const [draft, setDraft] = useState<Draft | null>(null)
  const [safeZone, setSafeZone] = useState(true)
  const [rendering, setRendering] = useState(false)
  const serverRef = useRef<ClipDetail | null>(null) // server version the draft was based on
  const videoRef = useRef<HTMLVideoElement>(null)
  const mobile = useIsMobile()
  const [mobileTab, setMobileTab] = useState('watch')

  // Take server data into the draft, but never throw away unsaved edits (a refetch after Approve or a
  // finished render must not wipe a half-edited caption)
  useEffect(() => {
    if (!clip) return
    setDraft((prev) => {
      const base = serverRef.current
      const unsaved = prev && base && JSON.stringify(draftOf(base)) !== JSON.stringify(prev)
      serverRef.current = clip
      return unsaved ? prev : draftOf(clip)
    })
  }, [clip])

  const adoptServer = (c: ClipDetail | undefined) => {
    if (!c) return
    serverRef.current = c
    setDraft(draftOf(c))
  }

  /** On Windows a file the <video> is still streaming can't be replaced: let go of it first. */
  const releaseVideo = () => {
    const v = videoRef.current
    if (!v) return
    v.pause()
    v.removeAttribute('src')
    v.load()
  }

  const { stages } = useJobEvents(jobId, (e) => {
    if (e.type === 'render_done' && e.ranks?.includes(clip?.rank ?? -1)) {
      setRendering(false)
      toast.success('Re-rendered!')
      refetch()
    }
    if (e.type === 'render_failed') {
      setRendering(false)
      toast.error('Render failed', { description: e.error })
    }
  })

  const dirty = useMemo(() => !!clip && !!draft && JSON.stringify(draftOf(clip)) !== JSON.stringify(draft), [clip, draft])
  const idx = allClips.findIndex((c) => c.id === clipId)
  const prev = idx > 0 ? allClips[idx - 1] : null
  const next = idx >= 0 && idx < allClips.length - 1 ? allClips[idx + 1] : null

  const save = useMutation({
    mutationFn: async () => {
      if (!draft || !clip) return null
      const body: Record<string, unknown> = { ...draft }
      // only send times when trimmed: the server re-snaps them (and resets word edits) when they change
      if (draft.start === clip.start && draft.end === clip.end) {
        delete body.start
        delete body.end
      }
      if (draft.layout === null) {
        delete body.layout
        body.clear_layout = true
      }
      return api.patchClip(clipId, body)
    },
    onSuccess: async () => {
      adoptServer((await refetch()).data) // the server may have snapped times: show its version
      qc.invalidateQueries({ queryKey: ['clips', jobId] })
      qc.invalidateQueries({ queryKey: ['shots', clipId] })
    },
    onError: (e) => toast.error(e.message),
  })
  const render = useMutation({
    mutationFn: async () => {
      if (dirty) await save.mutateAsync()
      releaseVideo()
      return api.renderClip(clipId)
    },
    onSuccess: () => {
      setRendering(true)
      toast('Rendering…', { description: 'Only this clip is re-rendered.' })
    },
    onError: (e) => toast.error(e.message),
  })
  const undo = useMutation({
    mutationFn: () => {
      releaseVideo()
      return api.undoClip(clipId)
    },
    onSuccess: async () => {
      toast.success('Restored previous version')
      adoptServer((await refetch()).data)
      qc.invalidateQueries({ queryKey: ['clips', jobId] })
    },
    onError: (e) => toast.error(e.message),
  })
  const approve = useMutation({
    mutationFn: (v: boolean | null) => api.patchClip(clipId, { approved: v }),
    onSuccess: () => {
      refetch()
      qc.invalidateQueries({ queryKey: ['clips', jobId] })
    },
  })

  if (error) return <ErrorBox error={error} />
  if (!clip || !draft) return <Skeleton className="h-[80vh] rounded-2xl" />
  const set = <K extends keyof Draft>(k: K, v: Draft[K]) => setDraft((d) => (d ? { ...d, [k]: v } : d))
  const style = draft.caption_style
  const rangeChanged = draft.start !== clip.start || draft.end !== clip.end
  const renderProgress = stages.render?.status === 'running' ? stages.render.progress : stages.reframe?.status === 'running' ? 0.05 : 0

  const textPanel = (<>
                <div className="space-y-2">
                  <Label htmlFor="hook">Hook title (on screen, first {3}s)</Label>
                  <Input id="hook" value={draft.hook} onChange={(e) => set('hook', e.target.value)} maxLength={80} />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="title">Clip title (file name & metadata)</Label>
                  <Textarea id="title" rows={2} value={draft.title} maxLength={100} onChange={(e) => set('title', e.target.value)} />
                  <div className={cn('text-right text-xs', draft.title.length > 60 ? 'text-destructive' : 'text-muted-foreground')}>{draft.title.length}/60</div>
                </div>
                <div className="space-y-2">
                  <div className="flex items-end justify-between">
                    <Label>Caption words</Label>
                    <span className="text-xs text-muted-foreground">Click a word to fix it · empty = remove · timing is kept</span>
                  </div>
                  {rangeChanged ? (
                    <p className="rounded-lg border border-dashed p-3 text-sm text-muted-foreground">Save the new start/end first to edit words.</p>
                  ) : (
                    <WordEditor clip={clip} edits={draft.word_edits} onChange={(e) => set('word_edits', e)} />
                  )}
                </div>
                {clip.why && <p className="text-xs text-muted-foreground"><span className="font-medium">Why the AI picked it:</span> {clip.why}</p>}
              </>)
  const stylePanel = (<>
                <CaptionStylePicker value={style.preset ?? 'bold-yellow-pop'} onChange={(k) => set('caption_style', { ...style, preset: k })} />
                <div className="grid gap-5 sm:grid-cols-2">
                  <div className="space-y-2">
                    <Label>Highlight colour</Label>
                    <div className="flex items-center gap-2">
                      <input type="color" className="h-9 w-12 cursor-pointer rounded border bg-transparent" value={style.highlight_color ?? '#FFE600'}
                        onChange={(e) => set('caption_style', { ...style, highlight_color: e.target.value })} />
                      <Button variant="ghost" size="sm" onClick={() => set('caption_style', { ...style, highlight_color: null })}>Preset default</Button>
                    </div>
                  </div>
                  <div className="space-y-2">
                    <Label>Keyword colour</Label>
                    <div className="flex items-center gap-2">
                      <input type="color" className="h-9 w-12 cursor-pointer rounded border bg-transparent" value={style.keyword_color ?? '#00FF88'}
                        onChange={(e) => set('caption_style', { ...style, keyword_color: e.target.value })} />
                      <Button variant="ghost" size="sm" onClick={() => set('caption_style', { ...style, keyword_color: null })}>Preset default</Button>
                    </div>
                  </div>
                  <div className="space-y-3">
                    <div className="flex justify-between text-sm"><Label>Font size</Label><span className="tabular-nums text-muted-foreground">{style.font_size ?? 'preset'}</span></div>
                    <Slider min={50} max={110} step={2} value={[style.font_size ?? 80]} onValueChange={([v]) => set('caption_style', { ...style, font_size: v })} />
                  </div>
                  <div className="space-y-3">
                    <div className="flex justify-between text-sm"><Label>Vertical position</Label><span className="tabular-nums text-muted-foreground">{Math.round((style.position_pct ?? 0.65) * 100)}%</span></div>
                    <Slider min={0.45} max={0.78} step={0.01} value={[style.position_pct ?? 0.65]} onValueChange={([v]) => set('caption_style', { ...style, position_pct: v })} />
                    <p className="text-xs text-muted-foreground">Keep below 78% so Reels/Shorts UI never covers captions.</p>
                  </div>
                </div>
              </>)
  const framingPanel = (<>
                <div className="space-y-2">
                  <Label>Layout for this clip</Label>
                  <ToggleGroup type="single" variant="outline" value={draft.layout ?? 'default'}
                    onValueChange={(v) => v && set('layout', v === 'default' ? null : (v as Layout))}>
                    <ToggleGroupItem value="default" className="px-3">Job default</ToggleGroupItem>
                    <ToggleGroupItem value="auto" className="px-3">Auto</ToggleGroupItem>
                    <ToggleGroupItem value="track" className="px-3">Track</ToggleGroupItem>
                    <ToggleGroupItem value="split" className="px-3">Split</ToggleGroupItem>
                    <ToggleGroupItem value="blur" className="px-3">Blur</ToggleGroupItem>
                  </ToggleGroup>
                </div>
                <div className="space-y-2">
                  <Label>Crop override per camera shot</Label>
                  <CropOverride clipId={clipId} value={draft.crop_overrides} onChange={(v) => set('crop_overrides', v)} />
                </div>
              </>)
  const trimmer = (
    <WaveformTrimmer
      key={clipId}
      jobId={jobId}
      start={draft.start}
      end={draft.end}
      sourceDuration={clip.source.duration}
      script={clip.script}
      mobile={mobile}
      onChange={(s, e) => setDraft((d) => (d ? { ...d, start: s, end: e } : d))}
    />
  )
  const approveBtn = (compact: boolean) => (
    <Button
      variant={clip.approved === true ? 'default' : 'outline'}
      size={compact ? 'icon' : 'default'}
      className={cn(clip.approved === true && 'bg-success text-black hover:bg-success/90', compact && 'size-11 shrink-0')}
      onClick={() => approve.mutate(clip.approved === true ? null : true)}
      aria-label="Approve"
    >
      <Check /> {!compact && (clip.approved === true ? 'Approved' : 'Approve')}
    </Button>
  )
  const rejectBtn = (compact: boolean) => (
    <Button variant={clip.approved === false ? 'destructive' : 'outline'} size={compact ? 'icon' : 'default'}
      className={cn(compact && 'size-11 shrink-0')} aria-label="Reject"
      onClick={() => approve.mutate(clip.approved === false ? null : false)}>
      <X /> {!compact && 'Reject'}
    </Button>
  )

  if (mobile) {
    // Phones: one thing at a time, sized for portrait; actions always under the thumb (no rotating the phone)
    return (
      <div className="-mx-4 -mt-6 pb-36">
        <div className="sticky top-[57px] z-20 border-b bg-background/95 px-3 pt-2 backdrop-blur">
          <div className="flex items-center gap-1">
            <Button variant="ghost" size="icon" asChild aria-label="Back to gallery">
              <Link to={`/jobs/${jobId}/clips`}><ArrowLeft /></Link>
            </Button>
            <span className="text-sm font-semibold">Clip #{clip.rank}</span>
            <ScoreBadge score={clip.score} />
            <span className="ml-1 truncate text-xs text-muted-foreground tabular-nums">{(draft.end - draft.start).toFixed(0)} s</span>
            <div className="ml-auto flex">
              <Button variant="ghost" size="icon" disabled={!prev} onClick={() => prev && nav(`/clips/${prev.id}/edit`)} aria-label="Previous clip"><ChevronLeft /></Button>
              <Button variant="ghost" size="icon" disabled={!next} onClick={() => next && nav(`/clips/${next.id}/edit`)} aria-label="Next clip"><ChevronRight /></Button>
            </div>
          </div>
          <Tabs value={mobileTab} onValueChange={setMobileTab}>
            <TabsList className="mt-1 mb-2 grid h-10 w-full grid-cols-5">
              <TabsTrigger value="watch">Watch</TabsTrigger>
              <TabsTrigger value="trim">Trim</TabsTrigger>
              <TabsTrigger value="text">Text</TabsTrigger>
              <TabsTrigger value="style">Style</TabsTrigger>
              <TabsTrigger value="framing">Frame</TabsTrigger>
            </TabsList>
          </Tabs>
        </div>

        <div className="px-4 pt-4">
          {mobileTab === 'watch' && (
            <div className="space-y-3">
              <PhoneFramePlayer
                ref={videoRef}
                src={videoSrc(clip.video_url)}
                poster={clip.thumbnail_url}
                safeZone={safeZone}
                className="max-w-[min(340px,calc((100dvh-15rem)*9/16))]"
                placeholder="Not rendered yet — tap Re-render."
              />
              <div className="mx-auto flex max-w-[340px] items-center justify-between text-sm">
                <Label htmlFor="safe-m" className="text-muted-foreground">Show Reels/Shorts UI</Label>
                <Switch id="safe-m" checked={safeZone} onCheckedChange={setSafeZone} />
              </div>
              <div className="mx-auto grid max-w-[340px] grid-cols-2 gap-2">
                <div className="[&>button]:w-full"><AddToQueueDialog jobId={jobId} ranks={[clip.rank]} label="Schedule" disabled={!clip.rendered} /></div>
                <Button variant="outline" asChild><Link to={`/clips/${clipId}/publish`}><Send /> Publish kit</Link></Button>
                <Button variant="ghost" className="col-span-2" onClick={() => undo.mutate()} disabled={!clip.has_previous || undo.isPending || rendering}>
                  <Undo2 /> Undo last render
                </Button>
              </div>
              {clip.why && <p className="text-xs text-muted-foreground"><span className="font-medium">Why the AI picked it:</span> {clip.why}</p>}
            </div>
          )}
          {mobileTab === 'trim' && (
            <div className="space-y-3">
              {trimmer}
              <p className="text-xs text-muted-foreground">The video above is the original episode: play the selection, set the start/end, then tap <b>Save &amp; re-render</b> to see the finished clip in Watch.</p>
            </div>
          )}
          {mobileTab === 'text' && <div className="space-y-5">{textPanel}</div>}
          {mobileTab === 'style' && <div className="space-y-6">{stylePanel}</div>}
          {mobileTab === 'framing' && <div className="space-y-6">{framingPanel}</div>}
        </div>

        {/* action bar above the app's bottom navigation */}
        <div className="fixed inset-x-0 bottom-[calc(3.5rem+env(safe-area-inset-bottom))] z-30 border-t bg-background/95 px-3 py-2 backdrop-blur">
          {rendering && <Progress value={Math.max(3, renderProgress * 100)} className="mb-2 h-1.5" />}
          {!rendering && clip.stale && !dirty && <p className="mb-1 text-[11px] text-primary">Saved edits aren't in the video yet — re-render.</p>}
          <div className="flex items-center gap-2">
            {approveBtn(true)}
            {rejectBtn(true)}
            {dirty && <Button variant="ghost" className="h-11 px-2" onClick={() => setDraft(draftOf(clip))}>Discard</Button>}
            <Button className="h-11 flex-1" onClick={() => { render.mutate(); setMobileTab('watch') }} disabled={rendering || render.isPending}>
              {rendering || render.isPending ? <Loader2 className="animate-spin" /> : <RefreshCw />}
              {rendering ? 'Rendering…' : dirty ? 'Save & re-render' : 'Re-render'}
            </Button>
          </div>
        </div>
      </div>
    )
  }

  return (
    <>
      <div className="mb-5 flex flex-wrap items-center gap-2">
        <Button variant="ghost" size="sm" asChild>
          <Link to={`/jobs/${jobId}/clips`}><ArrowLeft /> Gallery</Link>
        </Button>
        <div className="flex items-center gap-2">
          <span className="text-sm font-semibold">Clip #{clip.rank}</span>
          <ScoreBadge score={clip.score} />
          {clip.topic && <span className="text-xs text-muted-foreground">{clip.topic}</span>}
        </div>
        <div className="ml-auto flex items-center gap-1">
          <Button variant="ghost" size="icon" disabled={!prev} onClick={() => prev && nav(`/clips/${prev.id}/edit`)} aria-label="Previous clip"><ChevronLeft /></Button>
          <Button variant="ghost" size="icon" disabled={!next} onClick={() => next && nav(`/clips/${next.id}/edit`)} aria-label="Next clip"><ChevronRight /></Button>
        </div>
      </div>

      <div className="grid gap-6 lg:grid-cols-[minmax(260px,340px)_1fr]">
        {/* left: phone preview */}
        <div className="space-y-4">
          <PhoneFramePlayer
            ref={videoRef}
            src={videoSrc(clip.video_url)}
            poster={clip.thumbnail_url}
            safeZone={safeZone}
            className="max-w-[340px]"
            placeholder="Not rendered yet — click Re-render."
          />
          <div className="mx-auto flex max-w-[340px] items-center justify-between text-sm">
            <Label htmlFor="safe" className="text-muted-foreground">Show Reels/Shorts UI</Label>
            <Switch id="safe" checked={safeZone} onCheckedChange={setSafeZone} />
          </div>
          <div className="mx-auto grid max-w-[340px] grid-cols-2 gap-2">
            {approveBtn(false)}
            {rejectBtn(false)}
            <div className="col-span-2 [&>button]:w-full"><AddToQueueDialog jobId={jobId} ranks={[clip.rank]} label="Add to schedule" disabled={!clip.rendered} /></div>
            <Button variant="outline" className="col-span-2" asChild>
              <Link to={`/clips/${clipId}/publish`}><Send /> Publish kit</Link>
            </Button>
          </div>
        </div>

        {/* right: edit panel */}
        <Card className="gap-0 py-0">
          <CardContent className="p-0">
            <div className="flex flex-wrap items-center gap-2 border-b p-4">
              <Button onClick={() => render.mutate()} disabled={rendering || render.isPending}>
                {rendering || render.isPending ? <Loader2 className="animate-spin" /> : <RefreshCw />}
                {rendering ? 'Rendering…' : dirty ? 'Save & re-render' : 'Re-render'}
              </Button>
              <Button variant="outline" onClick={() => save.mutate()} disabled={!dirty || save.isPending}>
                <Save /> Save
              </Button>
              {dirty && (
                <Button variant="ghost" onClick={() => setDraft(draftOf(clip))}>Discard</Button>
              )}
              <Button variant="ghost" className="ml-auto" onClick={() => undo.mutate()} disabled={!clip.has_previous || undo.isPending || rendering}>
                <Undo2 /> Undo last render
              </Button>
              {rendering && <Progress value={Math.max(3, renderProgress * 100)} className="mt-1 h-1.5 basis-full" />}
              {!rendering && clip.stale && !dirty && (
                <p className="basis-full text-xs text-primary">Saved edits are not in the video yet — re-render to apply them.</p>
              )}
            </div>
            <Tabs defaultValue="text" className="p-4">
              <TabsList className="mb-4">
                <TabsTrigger value="text">Text</TabsTrigger>
                <TabsTrigger value="style">Caption style</TabsTrigger>
                <TabsTrigger value="framing">Framing</TabsTrigger>
              </TabsList>

              <TabsContent value="text" className="space-y-5">{textPanel}</TabsContent>
              <TabsContent value="style" className="space-y-6">{stylePanel}</TabsContent>
              <TabsContent value="framing" className="space-y-6">{framingPanel}</TabsContent>
            </Tabs>
          </CardContent>
        </Card>
      </div>

      <Card className="mt-6">
        <CardContent>
          {trimmer}
        </CardContent>
      </Card>
    </>
  )
}
