import { useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Activity, CheckCheck, Clapperboard, FileArchive, RefreshCw } from 'lucide-react'
import { toast } from 'sonner'
import { api } from '@/api/client'
import { useClips, useJob, useJobEvents } from '@/api/hooks'
import ClipCard from '@/components/ClipCard'
import { EmptyState, ErrorBox, PageHeader } from '@/components/common'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs'

type Filter = 'all' | 'approved' | 'rejected' | 'pending'

export default function Gallery() {
  const jobId = Number(useParams().jobId)
  const qc = useQueryClient()
  const { data: job } = useJob(jobId)
  const { data: clips, isLoading, error } = useClips(jobId)
  useJobEvents(jobId, (e) => {
    if (e.type === 'render_done') toast.success('Re-render finished')
    if (e.type === 'render_failed') toast.error('Render failed', { description: e.error })
  })
  const [filter, setFilter] = useState<Filter>('all')
  const [sort, setSort] = useState<'score' | 'time'>('score')
  const [minScore, setMinScore] = useState('7')

  const shown = useMemo(() => {
    let list = clips ?? []
    if (filter === 'approved') list = list.filter((c) => c.approved === true)
    if (filter === 'rejected') list = list.filter((c) => c.approved === false)
    if (filter === 'pending') list = list.filter((c) => c.approved == null)
    return [...list].sort((a, b) => (sort === 'score' ? b.score - a.score : a.start - b.start))
  }, [clips, filter, sort])

  const counts = useMemo(() => {
    const l = clips ?? []
    return {
      all: l.length,
      approved: l.filter((c) => c.approved === true).length,
      rejected: l.filter((c) => c.approved === false).length,
      pending: l.filter((c) => c.approved == null).length,
      stale: l.filter((c) => c.stale || !c.rendered).length,
    }
  }, [clips])

  const bulk = useMutation({
    mutationFn: () => api.bulkApprove(jobId, Number(minScore)),
    onSuccess: (r) => {
      toast.success(`Approved ${r.changed} clips`)
      qc.invalidateQueries({ queryKey: ['clips', jobId] })
    },
    onError: (e) => toast.error(e.message),
  })
  const renderAll = useMutation({
    mutationFn: () => api.renderAll(jobId),
    onSuccess: () => toast.success('Rendering queued — clips update as they finish'),
    onError: (e) => toast.error(e.message),
  })

  return (
    <>
      <PageHeader
        title={job?.title || 'Clips'}
        description={job ? `${counts.all} clips · ${counts.approved} approved${job.busy ? ' · processing…' : ''}` : undefined}
        actions={
          <>
            <Button variant="outline" asChild>
              <Link to={`/jobs/${jobId}`}><Activity /> Progress</Link>
            </Button>
            <Popover>
              <PopoverTrigger asChild>
                <Button variant="outline"><CheckCheck /> Approve ≥ score</Button>
              </PopoverTrigger>
              <PopoverContent className="w-64 space-y-3">
                <div className="text-sm font-medium">Approve all clips with score at least</div>
                <Input type="number" step="0.5" min="0" max="10" value={minScore} onChange={(e) => setMinScore(e.target.value)} />
                <Button className="w-full" onClick={() => bulk.mutate()} disabled={bulk.isPending}>Approve</Button>
              </PopoverContent>
            </Popover>
            <Button variant="outline" onClick={() => renderAll.mutate()} disabled={renderAll.isPending || job?.busy}>
              <RefreshCw /> Render {counts.stale ? `changed (${counts.stale})` : 'all'}
            </Button>
            <Button asChild>
              <a href={`/api/jobs/${jobId}/export.zip`}>
                <FileArchive /> Export ZIP {counts.approved ? `(${counts.approved})` : ''}
              </a>
            </Button>
          </>
        }
      />
      <div className="mb-5 flex flex-wrap items-center justify-between gap-3">
        <Tabs value={filter} onValueChange={(v) => setFilter(v as Filter)}>
          <TabsList>
            <TabsTrigger value="all">All {counts.all}</TabsTrigger>
            <TabsTrigger value="approved">Approved {counts.approved}</TabsTrigger>
            <TabsTrigger value="pending">To review {counts.pending}</TabsTrigger>
            <TabsTrigger value="rejected">Rejected {counts.rejected}</TabsTrigger>
          </TabsList>
        </Tabs>
        <Select value={sort} onValueChange={(v) => setSort(v as 'score' | 'time')}>
          <SelectTrigger className="w-40"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="score">Sort by score</SelectItem>
            <SelectItem value="time">Sort by time</SelectItem>
          </SelectContent>
        </Select>
      </div>
      {error ? (
        <ErrorBox error={error} />
      ) : isLoading ? (
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5">
          {Array.from({ length: 10 }).map((_, i) => <Skeleton key={i} className="aspect-[9/16] rounded-2xl" />)}
        </div>
      ) : !shown.length ? (
        <EmptyState icon={Clapperboard} title={filter === 'all' ? 'No clips yet' : `No ${filter} clips`}>
          {filter === 'all' ? 'Clips appear once the AI has selected them.' : 'Change the filter to see other clips.'}
        </EmptyState>
      ) : (
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5">
          {shown.map((c) => <ClipCard key={c.id} clip={c} />)}
        </div>
      )}
    </>
  )
}
