import { useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ChevronDown, Grid3x3, Loader2, RotateCcw, Square, Terminal } from 'lucide-react'
import { toast } from 'sonner'
import { api } from '@/api/client'
import { formatDuration, useClips, useJob, useJobEvents } from '@/api/hooks'
import { ErrorBox, PageHeader, StatusBadge } from '@/components/common'
import ClipCard from '@/components/ClipCard'
import ProgressStepper from '@/components/ProgressStepper'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Skeleton } from '@/components/ui/skeleton'
import { cn } from '@/lib/utils'

export default function JobProgress() {
  const jobId = Number(useParams().jobId)
  const qc = useQueryClient()
  const { data: job, error } = useJob(jobId)
  const { data: clips = [] } = useClips(jobId)
  const { stages, logs, connected } = useJobEvents(jobId, (e) => {
    if (e.type === 'status' && e.status === 'done') toast.success('All clips are ready!')
    if (e.type === 'status' && e.status === 'failed') toast.error('Job failed', { description: e.error })
  })
  const [logOpen, setLogOpen] = useState(false)
  const logRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (logOpen) logRef.current?.scrollTo({ top: logRef.current.scrollHeight })
  }, [logs, logOpen])

  const cancel = useMutation({
    mutationFn: () => api.cancelJob(jobId),
    onSuccess: () => toast('Cancelling…'),
    onError: (e) => toast.error(e.message),
  })
  const retry = useMutation({
    mutationFn: () => api.retryJob(jobId),
    onSuccess: () => {
      toast.success('Restarted — finished stages are reused')
      qc.invalidateQueries({ queryKey: ['job', jobId] })
    },
    onError: (e) => toast.error(e.message),
  })

  if (error) return <ErrorBox error={error} />
  if (!job) return <Skeleton className="h-96 rounded-2xl" />
  const rendered = clips.filter((c) => c.rendered)

  return (
    <>
      <PageHeader
        title={job.title || 'Preparing…'}
        description={
          <span className="flex flex-wrap items-center gap-2">
            <StatusBadge status={job.status} />
            {job.channel && <span>{job.channel}</span>}
            {job.duration > 0 && <span>· {formatDuration(job.duration)}</span>}
            {!connected && job.busy && <span className="text-xs">· reconnecting…</span>}
          </span>
        }
        actions={
          <>
            {job.busy && job.status !== 'done' && (
              <Button variant="outline" onClick={() => cancel.mutate()} disabled={cancel.isPending}>
                <Square /> Cancel
              </Button>
            )}
            {(job.status === 'failed' || job.status === 'cancelled') && !job.busy && (
              <Button onClick={() => retry.mutate()} disabled={retry.isPending}>
                <RotateCcw /> Retry
              </Button>
            )}
            {clips.length > 0 && (
              <Button variant={job.status === 'done' ? 'default' : 'outline'} asChild>
                <Link to={`/jobs/${jobId}/clips`}>
                  <Grid3x3 /> Open gallery
                </Link>
              </Button>
            )}
          </>
        }
      />

      {job.status === 'failed' && job.error && (
        <div className="mb-6">
          <ErrorBox error={job.error} />
        </div>
      )}

      <div className="grid gap-6 lg:grid-cols-[360px_1fr]">
        <div className="space-y-4">
          <Card className="py-3">
            <CardContent className="px-2">
              <ProgressStepper stages={stages} failed={job.status === 'failed'} />
            </CardContent>
          </Card>
          <Collapsible open={logOpen} onOpenChange={setLogOpen}>
            <Card className="gap-0 py-0">
              <CollapsibleTrigger asChild>
                <button className="flex w-full items-center justify-between px-5 py-4 text-sm font-medium">
                  <span className="flex items-center gap-2">
                    <Terminal className="size-4" /> Live log
                    <span className="text-xs font-normal text-muted-foreground">({logs.length})</span>
                  </span>
                  <ChevronDown className={cn('size-4 transition-transform', logOpen && 'rotate-180')} />
                </button>
              </CollapsibleTrigger>
              <CollapsibleContent>
                <div ref={logRef} className="scrollbar-thin max-h-80 overflow-auto border-t bg-black/30 px-4 py-3 font-mono text-[11px] leading-relaxed">
                  {logs.length === 0 && <div className="text-muted-foreground">Waiting for output…</div>}
                  {logs.map((l) => (
                    <div key={l.seq} className={cn(l.level === 'error' && 'text-destructive', l.level === 'warning' && 'text-yellow-400')}>
                      <span className="text-muted-foreground">{new Date(l.ts * 1000).toLocaleTimeString()} </span>
                      {l.message}
                    </div>
                  ))}
                </div>
              </CollapsibleContent>
            </Card>
          </Collapsible>
        </div>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center justify-between">
              <span>Clips</span>
              <span className="text-sm font-normal text-muted-foreground">
                {rendered.length}/{clips.length || '—'} rendered
              </span>
            </CardTitle>
          </CardHeader>
          <CardContent>
            {clips.length === 0 ? (
              <div className="flex flex-col items-center gap-3 py-16 text-center text-sm text-muted-foreground">
                {job.busy ? <Loader2 className="size-6 animate-spin" /> : null}
                Clips appear here as soon as the AI has picked them, and play as each one finishes rendering.
              </div>
            ) : (
              <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 xl:grid-cols-4">
                {clips.map((c) => (
                  <ClipCard key={c.id} clip={c} compact />
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    </>
  )
}
