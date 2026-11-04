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
  // zoom so the clip fills ~60% of the visible strip (both handles on screen), but keep words readable
  const [PX_PER_SEC] = useState(() => Math.max(28, Math.min(70, ((window.innerWidth - 320) * 0.6) / Math.max(5, end - start))))
  const containerRef = useRef<HTMLDivElement>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
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
      // centre the clip in the scroll strip
      const sc = scrollRef.current
      if (sc) sc.scrollLeft = ((start + end) / 2 - win.a) * PX_PER_SEC - sc.clientWidth / 2
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

  // ±0.5 s nudges for fingers (snap to the nearest word boundary; step further if the snap doesn't move)
  const nudge = (edge: 'start' | 'end', delta: number) => {
    const cur = edge === 'start' ? start : end
    let next = cur
    for (const k of [1, 2, 3, 4]) {
      next = snap(Math.max(win.a, Math.min(win.b, cur + delta * k)), edge)
      if (Math.abs(next - cur) > 0.05) break
    }
    const s = edge === 'start' ? next : start
    const e = edge === 'end' ? next : end
    if (e - s < 5) return
    cb.current(Math.round(s * 1000) / 1000, Math.round(e * 1000) / 1000)
  }

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
        <div className="ml-auto hidden text-xs text-muted-foreground sm:block">Drag the yellow box or its edges · snaps to words</div>
      </div>
      <div className="grid grid-cols-2 gap-2 sm:flex sm:flex-wrap" aria-label="Fine trim">
        <div className="flex items-center gap-1">
          <span className="w-10 text-xs text-muted-foreground">Start</span>
          <Button size="sm" variant="outline" className="h-10 flex-1 sm:h-8 sm:flex-none" onClick={() => nudge('start', -0.5)}>−0.5 s</Button>
          <Button size="sm" variant="outline" className="h-10 flex-1 sm:h-8 sm:flex-none" onClick={() => nudge('start', 0.5)}>+0.5 s</Button>
        </div>
        <div className="flex items-center gap-1">
          <span className="w-10 text-xs text-muted-foreground">End</span>
          <Button size="sm" variant="outline" className="h-10 flex-1 sm:h-8 sm:flex-none" onClick={() => nudge('end', -0.5)}>−0.5 s</Button>
          <Button size="sm" variant="outline" className="h-10 flex-1 sm:h-8 sm:flex-none" onClick={() => nudge('end', 0.5)}>+0.5 s</Button>
        </div>
      </div>
      <div ref={scrollRef} className="scrollbar-thin overflow-x-auto rounded-xl border bg-muted/30">
        <div className="relative px-0 pt-2 pb-1" style={{ width: len * PX_PER_SEC }}>
          {wave.isLoading ? <Skeleton className="h-24" /> : <div ref={containerRef} data-wave="" />}
          {playhead != null && playhead >= win.a && playhead <= win.b && (
            <div className="pointer-events-none absolute top-2 h-24 w-0.5 bg-primary" style={{ left: (playhead - win.a) * PX_PER_SEC }} />
          )}
          <div className="relative mt-1 h-12 text-[11px] text-muted-foreground">
            {(words.data ?? []).map((w, i) => {
              const inside = w.start >= start - 0.01 && w.end <= end + 0.01
              return (
                <span
                  key={i}
                  className={inside ? 'absolute whitespace-nowrap text-foreground' : 'absolute whitespace-nowrap'}
                  style={{ left: (w.start - win.a) * PX_PER_SEC, top: (i % 3) * 15 }}
                  title={`${formatTime(w.start)} ${w.text}`}
                >
                  {w.text}
                </span>
              )
            })}
          </div>
          <div className="flex justify-between px-1 text-[10px] text-muted-foreground tabular-nums">
            <span>{formatTime(win.a)}</span>
            <span>{formatTime(win.b)}</span>
          </div>
        </div>
      </div>
      <video ref={audioRef} src={`/api/jobs/${jobId}/source-video`} preload="none" className="hidden" onPause={() => setPlaying(false)} />
    </div>
  )
}
