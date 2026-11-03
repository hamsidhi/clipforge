import { Link, useNavigate } from 'react-router-dom'
import { Film, MoreVertical, Plus, RotateCcw, Trash2 } from 'lucide-react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { api } from '@/api/client'
import { formatDuration, STAGE_LABELS, useJobs } from '@/api/hooks'
import type { Job } from '@/api/types'
import { EmptyState, ErrorBox, PageHeader, StatusBadge } from '@/components/common'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { Progress } from '@/components/ui/progress'
import { Skeleton } from '@/components/ui/skeleton'

function jobLink(j: Job) {
  return j.status === 'done' || j.n_clips > 0 ? `/jobs/${j.id}/clips` : `/jobs/${j.id}`
}

function JobCard({ job }: { job: Job }) {
  const qc = useQueryClient()
  const nav = useNavigate()
  const del = useMutation({
    mutationFn: (files: boolean) => api.deleteJob(job.id, files),
    onSuccess: () => {
      toast.success('Job deleted')
      qc.invalidateQueries({ queryKey: ['jobs'] })
    },
    onError: (e) => toast.error(e.message),
  })
  const retry = useMutation({
    mutationFn: () => api.retryJob(job.id),
    onSuccess: () => nav(`/jobs/${job.id}`),
    onError: (e) => toast.error(e.message),
  })
  const created = new Date(job.created_at + (job.created_at.endsWith('Z') ? '' : 'Z'))
  return (
    <Card className="group gap-0 overflow-hidden p-0 transition-colors hover:border-primary/40">
      <Link to={jobLink(job)} className="relative block aspect-video bg-muted">
        {job.thumbnail_url && job.video_id ? (
          <img src={job.thumbnail_url} alt="" className="size-full object-cover" onError={(e) => (e.currentTarget.style.display = 'none')} />
        ) : (
          <div className="grid size-full place-items-center">
            <Film className="size-8 text-muted-foreground/50" />
          </div>
        )}
        <div className="absolute top-2 left-2">
          <StatusBadge status={job.status} />
        </div>
        {job.duration > 0 && (
          <span className="absolute right-2 bottom-2 rounded bg-black/70 px-1.5 py-0.5 text-[11px] tabular-nums">{formatDuration(job.duration)}</span>
        )}
      </Link>
      <div className="flex items-start gap-2 p-4">
        <Link to={jobLink(job)} className="min-w-0 flex-1">
          <div className="line-clamp-2 font-medium leading-snug">{job.title || job.source}</div>
          <div className="mt-1 text-xs text-muted-foreground">
            {job.channel && <>{job.channel} · </>}
            {created.toLocaleDateString()} · {job.n_rendered}/{job.n_clips || '—'} clips
          </div>
          {job.busy && job.status === 'running' && (
            <div className="mt-3 space-y-1.5">
              <div className="flex justify-between text-xs text-muted-foreground">
                <span>{STAGE_LABELS[job.stage] ?? job.stage}</span>
                <span>{Math.round(job.progress * 100)}%</span>
              </div>
              <Progress value={job.progress * 100} className="h-1.5" />
            </div>
          )}
          {job.status === 'failed' && <div className="mt-2 line-clamp-2 text-xs text-destructive">{job.error}</div>}
        </Link>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" size="icon" className="-mr-2 size-8 shrink-0" aria-label="Job actions">
              <MoreVertical />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            {(job.status === 'failed' || job.status === 'cancelled') && (
              <DropdownMenuItem onClick={() => retry.mutate()}>
                <RotateCcw /> Retry (resumes)
              </DropdownMenuItem>
            )}
            <DropdownMenuItem onClick={() => del.mutate(false)}>
              <Trash2 /> Remove from list
            </DropdownMenuItem>
            <DropdownMenuItem
              className="text-destructive focus:text-destructive"
              onClick={() => {
                if (confirm('Delete this job AND its video, transcript and clips from disk?')) del.mutate(true)
              }}
            >
              <Trash2 /> Delete with files
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </Card>
  )
}

export default function Dashboard() {
  const { data, isLoading, error, refetch } = useJobs()
  return (
    <>
      <PageHeader
        title="Your clip jobs"
        description="Every podcast you've turned into shorts. Jobs survive restarts."
        actions={
          <Button asChild>
            <Link to="/new">
              <Plus /> New clip job
            </Link>
          </Button>
        }
      />
      {error ? (
        <ErrorBox error={error} action={<Button variant="outline" size="sm" onClick={() => refetch()}>Try again</Button>} />
      ) : isLoading ? (
        <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
          {Array.from({ length: 3 }).map((_, i) => (
            <div key={i} className="space-y-3">
              <Skeleton className="aspect-video rounded-xl" />
              <Skeleton className="h-4 w-3/4" />
              <Skeleton className="h-3 w-1/2" />
            </div>
          ))}
        </div>
      ) : !data?.length ? (
        <EmptyState
          icon={Film}
          title="No jobs yet"
          action={
            <Button asChild>
              <Link to="/new">
                <Plus /> Create your first job
              </Link>
            </Button>
          }
        >
          Paste a YouTube podcast link (or drop a video file) and ClipForge will cut 15–25 vertical shorts with captions.
        </EmptyState>
      ) : (
        <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
          {data.map((j) => (
            <JobCard key={j.id} job={j} />
          ))}
        </div>
      )}
    </>
  )
}
