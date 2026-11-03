import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import WaveSurfer from 'wavesurfer.js'
import RegionsPlugin, { type Region } from 'wavesurfer.js/plugins/regions'
import { Pause, Play } from 'lucide-react'
import { api } from '@/api/client'
import { formatTime } from '@/api/hooks'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'

const PAD = 30 // seconds of context on each side

interface Props {
  jobId: number
  start: number
  end: number
  sourceDuration: number
  script: string
  onChange: (start: number, end: number) => void
}

/** Source waveform around the clip (±30 s) with draggable start/end handles that snap to words. */
export default function WaveformTrimmer({ jobId, start, end, sourceDuration, script, onChange }: Props) {
  // window is fixed when the editor opens so dragging doesn't re-fetch constantly
  const [win] = useState(() => ({ a: Math.max(0, start - PAD), b: Math.min(sourceDuration || end + PAD, end + PAD) }))
  const containerRef = useRef<HTMLDivElement>(null)
  const regionRef = useRef<Region | null>(null)
  const audioRef = useRef<HTMLVideoElement>(null)
  const [playing, setPlaying] = useState(false)
  const [playhead, setPlayhead] = useState<number | null>(null)
  const cb = useRef(onChange)
  cb.current = onChange

  const wave = useQuery({ queryKey: ['wave', jobId, win.a, win.b], queryFn: () => api.waveform(jobId, win.a, win.b), staleTime: Infinity })
  const words = useQuery({
    queryKey: ['words', jobId, win.a, win.b, script],
    queryFn: () => api.transcript(jobId, win.a, win.b, script),
    staleTime: 30_000,
  })
  const len = win.b - win.a

  const snap = useMemo(() => {
    const ws = words.data ?? []
    return (t: number, edge: 'start' | 'end') => {
      if (!ws.length) return t
      let best = t
      let bestD = Infinity
      for (const w of ws) {
        const v = edge === 'start' ? w.start : w.end
        const d = Math.abs(v - t)
        if (d < bestD) {
          bestD = d
          best = v
        }
      }
      return bestD < 2 ? best : t
    }
  }, [words.data])
  const snapRef = useRef(snap)
  snapRef.current = snap

  useEffect(() => {
    if (!containerRef.current || !wave.data) return
    const regions = RegionsPlugin.create()
    const ws = WaveSurfer.create({
      container: containerRef.current,
      height: 96,
      waveColor: 'oklch(0.5 0.01 285)',
      progressColor: 'oklch(0.5 0.01 285)',
      cursorWidth: 0,
      barWidth: 2,
      barGap: 1,
      barRadius: 2,
      interact: false,
      peaks: [wave.data.peaks],
      duration: len,
      plugins: [regions],
    })
    ws.on('ready', () => {
      regionRef.current = regions.addRegion({
        start: start - win.a,
        end: end - win.a,
        color: 'oklch(0.91 0.19 103 / 0.18)',
        drag: true,
        resize: true,
        minLength: 5,
      })
    })
    regions.on('region-updated', (r) => {
      const s = snapRef.current(r.start + win.a, 'start')
      const e = snapRef.current(r.end + win.a, 'end')
      r.setOptions({ start: s - win.a, end: e - win.a })
      cb.current(Math.round(s * 1000) / 1000, Math.round(e * 1000) / 1000)
    })
    return () => {
      ws.destroy()
      regionRef.current = null
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wave.data, len, win.a])

  // external start/end changes (e.g. undo) move the region
  useEffect(() => {
    const r = regionRef.current
    if (r && (Math.abs(r.start + win.a - start) > 0.05 || Math.abs(r.end + win.a - end) > 0.05)) {
      r.setOptions({ start: start - win.a, end: end - win.a })
    }
  }, [start, end, win.a])

  // play selection through a hidden <video> of the source (audio only matters)
  useEffect(() => {
    const el = audioRef.current
    if (!el) return
    const tick = () => {
      setPlayhead(el.currentTime)
      if (el.currentTime >= end) {
        el.pause()
        setPlaying(false)
      }
    }
    el.addEventListener('timeupdate', tick)
    return () => el.removeEventListener('timeupdate', tick)
  }, [end])

  const toggle = async () => {
    const el = audioRef.current
    if (!el) return
    if (playing) {
      el.pause()
      setPlaying(false)
      return
    }
    el.currentTime = start
    try {
      await el.play()
      setPlaying(true)
    } catch {
      setPlaying(false)
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        <Button size="sm" variant="secondary" onClick={toggle}>
          {playing ? <Pause /> : <Play />} {playing ? 'Pause' : 'Play selection'}
        </Button>
        <div className="text-xs text-muted-foreground tabular-nums">
          {formatTime(start)} → {formatTime(end)} · <span className="font-medium text-foreground">{(end - start).toFixed(1)}s</span>
        </div>
        <div className="ml-auto text-xs text-muted-foreground">Drag the yellow box or its edges · snaps to words</div>
      </div>
      <div className="relative rounded-xl border bg-muted/30 px-2 pt-2 pb-1">
        {wave.isLoading ? <Skeleton className="h-24" /> : <div ref={containerRef} />}
        {playhead != null && playhead >= win.a && playhead <= win.b && (
          <div className="pointer-events-none absolute top-2 h-24 w-0.5 bg-primary" style={{ left: `calc(8px + (100% - 16px) * ${(playhead - win.a) / len})` }} />
        )}
        <div className="relative mt-1 h-10 overflow-hidden text-[10px] text-muted-foreground">
          {(words.data ?? []).map((w, i) => {
            const inside = w.start >= start - 0.01 && w.end <= end + 0.01
            return (
              <span
                key={i}
                className={inside ? 'absolute whitespace-nowrap text-foreground' : 'absolute whitespace-nowrap'}
                style={{ left: `${((w.start - win.a) / len) * 100}%`, top: i % 2 ? 18 : 2 }}
                title={`${formatTime(w.start)} ${w.text}`}
              >
                {w.text}
              </span>
            )
          })}
        </div>
        <div className="flex justify-between text-[10px] text-muted-foreground tabular-nums">
          <span>{formatTime(win.a)}</span>
          <span>{formatTime(win.b)}</span>
        </div>
      </div>
      <video ref={audioRef} src={`/api/jobs/${jobId}/source-video`} preload="none" className="hidden" onPause={() => setPlaying(false)} />
    </div>
  )
}
