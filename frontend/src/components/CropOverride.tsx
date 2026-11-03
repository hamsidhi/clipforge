import { useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { RotateCcw } from 'lucide-react'
import { api } from '@/api/client'
import { formatTime } from '@/api/hooks'
import type { CropOverride as Override } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { cn } from '@/lib/utils'

/** Per-shot manual crop: drag the 9:16 box over a source frame when face tracking picked wrong. */
export default function CropOverride({ clipId, value, onChange }: {
  clipId: string
  value: Override[]
  onChange: (v: Override[]) => void
}) {
  const { data, isLoading, error } = useQuery({ queryKey: ['shots', clipId], queryFn: () => api.shots(clipId) })
  if (isLoading) return <Skeleton className="h-40" />
  if (error || !data) return <p className="text-sm text-muted-foreground">Shots are available after the first render.</p>
  return (
    <div className="space-y-4">
      <p className="text-xs text-muted-foreground">
        {data.shots.length} camera shot{data.shots.length === 1 ? '' : 's'}. Drag the box to choose what stays in frame, then re-render.
      </p>
      {data.shots.map((shot) => {
        const ov = value.find((o) => o.shot_index === shot.index)
        return (
          <ShotCrop
            key={shot.index}
            frameUrl={shot.frame_url}
            label={`Shot ${shot.index + 1} · ${formatTime(shot.start)}–${formatTime(shot.end)} · ${ov ? 'manual' : shot.mode ?? 'auto'}`}
            cropW={data.crop_width_frac}
            cx={ov?.cx ?? shot.cx ?? 0.5}
            manual={!!ov}
            onChange={(cx) => onChange([...value.filter((o) => o.shot_index !== shot.index), { shot_index: shot.index, cx, cy: 0.5 }])}
            onReset={() => onChange(value.filter((o) => o.shot_index !== shot.index))}
          />
        )
      })}
    </div>
  )
}

function ShotCrop({ frameUrl, label, cropW, cx, manual, onChange, onReset }: {
  frameUrl: string
  label: string
  cropW: number
  cx: number
  manual: boolean
  onChange: (cx: number) => void
  onReset: () => void
}) {
  const ref = useRef<HTMLDivElement>(null)
  const [drag, setDrag] = useState<number | null>(null)
  const pos = drag ?? cx
  const clamp = (v: number) => Math.min(1 - cropW / 2, Math.max(cropW / 2, v))
  const fromEvent = (clientX: number) => {
    const r = ref.current!.getBoundingClientRect()
    return clamp((clientX - r.left) / r.width)
  }
  return (
    <div>
      <div className="mb-1.5 flex items-center justify-between text-xs">
        <span className={cn('text-muted-foreground', manual && 'text-primary')}>{label}</span>
        {manual && (
          <Button variant="ghost" size="sm" className="h-6 px-2 text-xs" onClick={onReset}>
            <RotateCcw className="size-3" /> auto
          </Button>
        )}
      </div>
      <div
        ref={ref}
        className="relative aspect-video touch-none overflow-hidden rounded-lg bg-black select-none"
        onPointerDown={(e) => {
          ;(e.target as HTMLElement).setPointerCapture(e.pointerId)
          setDrag(fromEvent(e.clientX))
        }}
        onPointerMove={(e) => drag != null && setDrag(fromEvent(e.clientX))}
        onPointerUp={() => {
          if (drag != null) onChange(Math.round(drag * 1000) / 1000)
          setDrag(null)
        }}
      >
        <img src={frameUrl} alt="" className="absolute inset-0 size-full object-cover opacity-50" draggable={false} />
        <div
          className="absolute inset-y-0 cursor-grab border-2 border-primary bg-transparent shadow-[0_0_0_9999px_rgb(0_0_0/0.55)] active:cursor-grabbing"
          style={{ left: `${(pos - cropW / 2) * 100}%`, width: `${cropW * 100}%` }}
        >
          <div className="absolute inset-0 overflow-hidden">
            <img
              src={frameUrl}
              alt=""
              draggable={false}
              className="absolute top-0 h-full max-w-none"
              style={{ width: `${100 / cropW}%`, left: `${-((pos - cropW / 2) / cropW) * 100}%` }}
            />
          </div>
        </div>
      </div>
    </div>
  )
}
