import { useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Check, Download, Loader2, Pencil, Send, X } from 'lucide-react'
import { toast } from 'sonner'
import { api, videoSrc } from '@/api/client'
import type { Clip } from '@/api/types'
import { ScoreBadge } from '@/components/common'
import { Button } from '@/components/ui/button'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'

export default function ClipCard({ clip, compact = false }: { clip: Clip; compact?: boolean }) {
  const qc = useQueryClient()
  const videoRef = useRef<HTMLVideoElement>(null)
  const [hover, setHover] = useState(false)
  const [dx, setDx] = useState(0)
  const start = useRef<{ x: number; y: number; id: number } | null>(null)
  const approve = useMutation({
    mutationFn: (approved: boolean | null) => api.patchClip(clip.id, { approved }),
    onMutate: async (approved) => {
      await qc.cancelQueries({ queryKey: ['clips', clip.job_id] })
      qc.setQueryData<Clip[]>(['clips', clip.job_id], (old) => old?.map((c) => (c.id === clip.id ? { ...c, approved } : c)))
    },
    onError: (e) => toast.error(e.message),
    onSettled: () => qc.invalidateQueries({ queryKey: ['clips', clip.job_id] }),
  })

  return (
    <div
      className={cn(
        'group flex flex-col overflow-hidden rounded-2xl border bg-card transition-all',
        clip.approved === true && 'border-success/60',
        clip.approved === false && 'opacity-50',
      )}
    >
      <div
        className="relative aspect-[9/16] touch-pan-y bg-black transition-transform"
        style={dx ? { transform: `translateX(${dx}px) rotate(${dx / 30}deg)`, transition: 'none' } : undefined}
        onPointerDown={(e) => {
          if (e.pointerType === 'touch' && !compact) start.current = { x: e.clientX, y: e.clientY, id: e.pointerId }
        }}
        onPointerMove={(e) => {
          const s = start.current
          if (!s || s.id !== e.pointerId) return
          const ddx = e.clientX - s.x
          if (Math.abs(ddx) > Math.abs(e.clientY - s.y)) setDx(Math.max(-140, Math.min(140, ddx)))
        }}
        onPointerUp={() => {
          // swipe right = approve, left = reject (phone gallery)
          if (dx > 80) approve.mutate(clip.approved === true ? null : true)
          else if (dx < -80) approve.mutate(clip.approved === false ? null : false)
          else if (start.current && Math.abs(dx) < 8 && videoRef.current) {  // tap = play / pause
            const v = videoRef.current
            if (v.paused) {
              setHover(true)
              v.play().catch(() => {})
            } else v.pause()
          }
          start.current = null
          setDx(0)
        }}
        onPointerCancel={() => {
          start.current = null
          setDx(0)
        }}
        onMouseEnter={() => {
          setHover(true)
          videoRef.current?.play().catch(() => {})
        }}
        onMouseLeave={() => {
          setHover(false)
          if (videoRef.current) {
            videoRef.current.pause()
            videoRef.current.currentTime = 0
          }
        }}
      >
        {clip.video_url ? (
          <>
            {clip.thumbnail_url && !hover && <img src={clip.thumbnail_url} alt="" className="absolute inset-0 size-full object-cover" />}
            <video
              ref={videoRef}
              src={videoSrc(clip.video_url) ?? undefined}
              muted
              loop
              playsInline
              preload="none"
              className={cn('absolute inset-0 size-full object-cover', !hover && clip.thumbnail_url && 'opacity-0')}
            />
          </>
        ) : (
          <div className="absolute inset-0 grid place-items-center text-xs text-muted-foreground">
            <div className="flex flex-col items-center gap-2">
              <Loader2 className="size-5 animate-spin" />
              rendering…
            </div>
          </div>
        )}
        <div className="absolute top-2 left-2 flex items-center gap-1.5">
          <span className="rounded-md bg-black/70 px-1.5 py-0.5 text-xs font-semibold">#{clip.rank}</span>
          <ScoreBadge score={clip.score} />
        </div>
        <span className="absolute top-2 right-2 rounded-md bg-black/70 px-1.5 py-0.5 text-xs tabular-nums">{Math.round(clip.duration)}s</span>
        {dx !== 0 && (
          <span className={cn('absolute inset-x-0 top-1/2 mx-auto w-fit -translate-y-1/2 rounded-lg px-3 py-1.5 text-sm font-semibold',
            dx > 0 ? 'bg-success text-black' : 'bg-destructive text-white', Math.abs(dx) < 80 && 'opacity-60')}>
            {dx > 0 ? 'Approve' : 'Reject'}
          </span>
        )}
        {clip.stale && clip.rendered && (
          <span className="absolute bottom-2 left-2 rounded-md bg-primary px-1.5 py-0.5 text-[10px] font-semibold text-primary-foreground">edited · re-render</span>
        )}
      </div>
      <div className="flex flex-1 flex-col gap-2 p-3">
        <Link to={`/clips/${clip.id}/edit`} className="line-clamp-2 text-sm leading-snug font-medium hover:underline">
          {clip.title}
        </Link>
        {!compact && clip.topic && <div className="text-xs text-muted-foreground">{clip.topic}</div>}
        {!compact && (
          <div className="mt-auto flex items-center gap-1 pt-1">
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  size="icon"
                  variant={clip.approved === true ? 'default' : 'outline'}
                  className={cn('size-10 md:size-8', clip.approved === true && 'bg-success text-black hover:bg-success/90')}
                  onClick={() => approve.mutate(clip.approved === true ? null : true)}
                  aria-label="Approve"
                >
                  <Check />
                </Button>
              </TooltipTrigger>
              <TooltipContent>Approve</TooltipContent>
            </Tooltip>
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  size="icon"
                  variant={clip.approved === false ? 'destructive' : 'outline'}
                  className="size-10 md:size-8"
                  onClick={() => approve.mutate(clip.approved === false ? null : false)}
                  aria-label="Reject"
                >
                  <X />
                </Button>
              </TooltipTrigger>
              <TooltipContent>Reject</TooltipContent>
            </Tooltip>
            <div className="ml-auto flex gap-1">
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button size="icon" variant="ghost" className="size-8" asChild>
                    <Link to={`/clips/${clip.id}/edit`} aria-label="Edit"><Pencil /></Link>
                  </Button>
                </TooltipTrigger>
                <TooltipContent>Edit</TooltipContent>
              </Tooltip>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button size="icon" variant="ghost" className="size-8" asChild>
                    <Link to={`/clips/${clip.id}/publish`} aria-label="Publish kit"><Send /></Link>
                  </Button>
                </TooltipTrigger>
                <TooltipContent>Publish kit</TooltipContent>
              </Tooltip>
              {clip.video_url && (
                <Tooltip>
                  <TooltipTrigger asChild>
                    <Button size="icon" variant="ghost" className="size-8" asChild>
                      <a href={`/api/clips/${clip.id}/video?download=true`} aria-label="Download"><Download /></a>
                    </Button>
                  </TooltipTrigger>
                  <TooltipContent>Download mp4</TooltipContent>
                </Tooltip>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
