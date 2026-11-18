import { useMemo, useState, type ReactNode } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { Film, Folder, FolderInput, FolderOpen, MoreVertical, Pencil, Plus, Radio, RotateCcw, Search, Trash2 } from 'lucide-react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { api } from '@/api/client'
import { formatDuration, STAGE_LABELS, useJobs } from '@/api/hooks'
import type { Job } from '@/api/types'
import { live, type LiveSessionRow } from '@/api/live'
import PlatformIcon from '@/components/PlatformIcon'
import { EmptyState, ErrorBox, PageHeader, StatusBadge } from '@/components/common'
import FolderPicker from '@/components/FolderPicker'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import { Progress } from '@/components/ui/progress'
import { Skeleton } from '@/components/ui/skeleton'
import { cn } from '@/lib/utils'

function jobLink(j: Job) {
  return j.status === 'done' || j.n_clips > 0 ? `/jobs/${j.id}/clips` : `/jobs/${j.id}`
}

function useFolderRefresh() {
  const qc = useQueryClient()
  return () => {
    qc.invalidateQueries({ queryKey: ['jobs'] })
    qc.invalidateQueries({ queryKey: ['job-folders'] })
  }
}

function MoveDialog({ job, onClose }: { job: Job; onClose: () => void }) {
  const refresh = useFolderRefresh()
  const [folder, setFolder] = useState(job.folder)
  const m = useMutation({
    mutationFn: () => api.moveJob(job.id, folder),
    onSuccess: () => {
      toast.success(folder ? `Moved to ${folder}` : 'Taken out of the folder')
      refresh()
      onClose()
    },
    onError: (e) => toast.error(e.message),
  })
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Move to folder</DialogTitle>
          <DialogDescription className="line-clamp-2">{job.title || job.source}</DialogDescription>
        </DialogHeader>
        <FolderPicker value={folder} onChange={setFolder} />
        <DialogFooter>
          <Button onClick={() => m.mutate()} disabled={m.isPending || folder === job.folder}><FolderInput /> Move</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function RenameDialog({ name, onClose, onDone }: { name: string; onClose: () => void; onDone: (n: string) => void }) {
  const refresh = useFolderRefresh()
  const [next, setNext] = useState(name)
  const m = useMutation({
    mutationFn: () => api.renameFolder(name, next.trim()),
    onSuccess: (r) => {
      refresh()
      toast.success(`Renamed (${r.moved} jobs)`)
      onDone(r.folder)
    },
    onError: (e) => toast.error(e.message),
  })
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Rename folder</DialogTitle>
          <DialogDescription>Using another folder's name merges the two.</DialogDescription>
        </DialogHeader>
        <Input autoFocus value={next} maxLength={60} onChange={(e) => setNext(e.target.value)} />
        <DialogFooter>
          <Button onClick={() => m.mutate()} disabled={!next.trim() || next.trim() === name || m.isPending}><Pencil /> Rename</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function JobCard({ job, showFolder }: { job: Job; showFolder: boolean }) {
  const qc = useQueryClient()
  const nav = useNavigate()
  const [moving, setMoving] = useState(false)
  const del = useMutation({
    mutationFn: (files: boolean) => api.deleteJob(job.id, files),
    onSuccess: () => {
      toast.success('Job deleted')
      qc.invalidateQueries({ queryKey: ['jobs'] })
      qc.invalidateQueries({ queryKey: ['job-folders'] })
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
        {showFolder && job.folder && (
          <span className="absolute top-2 right-2 inline-flex max-w-[60%] items-center gap-1 rounded bg-black/70 px-1.5 py-0.5 text-[11px]">
            <Folder className="size-3 shrink-0" /><span className="truncate">{job.folder}</span>
          </span>
        )}
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
            <DropdownMenuItem onClick={() => setMoving(true)}>
              <FolderInput /> Move to folder…
            </DropdownMenuItem>
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
      {moving && <MoveDialog job={job} onClose={() => setMoving(false)} />}
    </Card>
  )
}

function LiveMoveDialog({ row, onClose }: { row: LiveSessionRow; onClose: () => void }) {
  const refresh = useFolderRefresh()
  const qc = useQueryClient()
  const [folder, setFolder] = useState(row.folder)
  const m = useMutation({
    mutationFn: () => live.moveSession(row.id, folder),
    onSuccess: (r) => {
      toast.success(`Moved to ${r.folder}`)
      refresh()
      qc.invalidateQueries({ queryKey: ['live-sessions'] })
      onClose()
    },
    onError: (e) => toast.error(e.message),
  })
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Move live stream to folder</DialogTitle>
          <DialogDescription className="line-clamp-2">{row.title || row.streamer}</DialogDescription>
        </DialogHeader>
        <FolderPicker value={folder} onChange={setFolder} />
        <DialogFooter>
          <Button onClick={() => m.mutate()} disabled={m.isPending || !folder || folder === row.folder}><FolderInput /> Move</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/** A live stream that was clipped (Telegram /live or the Live page): its clips live on the Live page. */
function LiveCard({ row, showFolder }: { row: LiveSessionRow; showFolder: boolean }) {
  const [moving, setMoving] = useState(false)
  const started = row.started_at ? new Date(row.started_at) : null
  const thumb = row.platform === 'youtube' && /[?&/]v[=/]|\/live\//.test(row.stream_url)
    ? (row.stream_url.match(/(?:v=|\/live\/)([\w-]{11})/)?.[1] ?? '') : ''
  return (
    <Card className="group gap-0 overflow-hidden p-0 transition-colors hover:border-primary/40">
      <Link to={`/live#${row.id}`} className="relative block aspect-video bg-muted">
        {thumb ? (
          <img src={`https://i.ytimg.com/vi/${thumb}/hqdefault.jpg`} alt="" className="size-full object-cover"
            onError={(e) => (e.currentTarget.style.display = 'none')} />
        ) : (
          <div className="grid size-full place-items-center"><Radio className="size-8 text-muted-foreground/50" /></div>
        )}
        <span className={cn('absolute top-2 left-2 inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-medium',
          row.live && !row.ended_at ? 'bg-red-600 text-white' : 'bg-black/70')}>
          <Radio className="size-3" /> {row.live && !row.ended_at ? 'LIVE' : 'Live stream'}
        </span>
        {showFolder && (
          <span className="absolute top-2 right-2 inline-flex max-w-[60%] items-center gap-1 rounded bg-black/70 px-1.5 py-0.5 text-[11px]">
            <Folder className="size-3 shrink-0" /><span className="truncate">{row.folder}</span>
          </span>
        )}
      </Link>
      <div className="flex items-start gap-2 p-4">
        <Link to={`/live#${row.id}`} className="min-w-0 flex-1">
          <div className="line-clamp-2 font-medium leading-snug">{row.title || row.streamer || row.stream_url}</div>
          <div className="mt-1 flex items-center gap-1 text-xs text-muted-foreground">
            {row.platform && <PlatformIcon platform={row.platform as never} className="size-3" />}
            {row.streamer && <>{row.streamer} · </>}
            {started && <>{started.toLocaleDateString()} · </>}
            {row.n_clips} clips
          </div>
        </Link>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" size="icon" className="-mr-2 size-8 shrink-0" aria-label="Live stream actions">
              <MoreVertical />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem onClick={() => setMoving(true)}><FolderInput /> Move to folder…</DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
      {moving && <LiveMoveDialog row={row} onClose={() => setMoving(false)} />}
    </Card>
  )
}

const ALL = '__all__'
const NONE = '__none__'

export default function Dashboard() {
  const { data, isLoading, error, refetch } = useJobs()
  const [params, setParams] = useSearchParams()
  const sel = params.get('folder') ?? ALL
  const setSel = (f: string) => setParams(f === ALL ? {} : { folder: f }, { replace: true })
  const [q, setQ] = useState('')
  const [renaming, setRenaming] = useState(false)
  const jobs = useMemo(() => data ?? [], [data])
  const { data: liveData } = useQuery({ queryKey: ['live-sessions'], queryFn: live.sessions, refetchInterval: 15000 })
  const lives = useMemo(() => liveData ?? [], [liveData])
  const folders = useMemo(() => {
    const m = new Map<string, { n: number; busy: number }>()
    const add = (f: string, busy: boolean) => {
      const key = [...m.keys()].find((k) => k.toLowerCase() === f.toLowerCase()) ?? f
      const e = m.get(key) ?? { n: 0, busy: 0 }
      e.n += 1
      if (busy) e.busy += 1
      m.set(key, e)
    }
    for (const j of jobs) if (j.folder) add(j.folder, j.busy)
    for (const l of lives) add(l.folder, l.live && !l.ended_at)
    return [...m.entries()].sort((a, b) => a[0].localeCompare(b[0], undefined, { sensitivity: 'base' }))
  }, [jobs, lives])
  const unfiled = jobs.filter((j) => !j.folder).length
  const needle = q.trim().toLowerCase()
  const shown = jobs.filter((j) =>
    (sel === ALL || (sel === NONE ? !j.folder : j.folder === sel)) &&
    (!needle || `${j.title} ${j.source} ${j.channel} ${j.folder}`.toLowerCase().includes(needle)))
  const shownLive = lives.filter((l) =>
    (sel === ALL || (sel !== NONE && l.folder.toLowerCase() === sel.toLowerCase())) &&
    (!needle || `${l.title} ${l.streamer} ${l.folder}`.toLowerCase().includes(needle)))
  const isFolder = sel !== ALL && sel !== NONE
  const tab = (key: string, label: string, n: number, icon: ReactNode, busy = 0) => (
    <button key={key} type="button" onClick={() => setSel(key)}
      className={cn('flex shrink-0 items-center gap-2 rounded-lg border px-3 py-2 text-sm transition-colors',
        sel === key ? 'border-primary bg-primary/10 text-foreground' : 'border-border text-muted-foreground hover:border-primary/40 hover:text-foreground')}>
      {icon}
      <span className="max-w-40 truncate">{label}</span>
      <span className="rounded-full bg-muted px-1.5 text-[11px] tabular-nums">{n}</span>
      {busy > 0 && <span className="size-1.5 rounded-full bg-primary" title="running" />}
    </button>
  )
  const newLink = isFolder ? `/new?folder=${encodeURIComponent(sel)}` : '/new'
  return (
    <>
      <PageHeader
        title="Your clip jobs"
        description="Every video you've turned into shorts, sorted into folders."
        actions={
          <Button asChild>
            <Link to={newLink}>
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
      ) : !jobs.length && !lives.length ? (
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
        <>
          <div className="mb-4 flex gap-2 overflow-x-auto pb-1">
            {tab(ALL, 'All jobs', jobs.length + lives.length, <Film className="size-4" />,
              jobs.filter((j) => j.busy).length + lives.filter((l) => l.live && !l.ended_at).length)}
            {folders.map(([name, e]) =>
              tab(name, name, e.n, sel === name ? <FolderOpen className="size-4" /> : <Folder className="size-4" />, e.busy))}
            {unfiled > 0 && folders.length > 0 && tab(NONE, 'No folder', unfiled, <Folder className="size-4 opacity-50" />)}
          </div>
          <div className="mb-5 flex flex-wrap items-center gap-2">
            <div className="relative min-w-0 flex-1 sm:max-w-xs">
              <Search className="absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" />
              <Input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search jobs" className="pl-8" />
            </div>
            {isFolder && (
              <Button variant="outline" size="sm" onClick={() => setRenaming(true)}><Pencil /> Rename folder</Button>
            )}
            {!folders.length && (
              <span className="text-xs text-muted-foreground">Tip: ⋮ on a job → Move to folder, or pick a folder when you start a job.</span>
            )}
          </div>
          {!shown.length && !shownLive.length ? (
            <EmptyState icon={FolderOpen} title={needle ? 'Nothing matches' : 'This folder is empty'}>
              {needle ? 'Try another word.' : 'Start a job and pick this folder.'}
            </EmptyState>
          ) : (
            <div className="grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
              {shownLive.map((l) => <LiveCard key={l.id} row={l} showFolder={sel === ALL} />)}
              {shown.map((j) => (
                <JobCard key={j.id} job={j} showFolder={sel === ALL} />
              ))}
            </div>
          )}
          {renaming && isFolder && (
            <RenameDialog name={sel} onClose={() => setRenaming(false)} onDone={(n) => { setRenaming(false); setSel(n || ALL) }} />
          )}
        </>
      )}
    </>
  )
}
