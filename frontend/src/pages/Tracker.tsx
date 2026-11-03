import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { BarChart3, Download, ExternalLink, Plus, Trash2 } from 'lucide-react'
import { toast } from 'sonner'
import { api } from '@/api/client'
import type { Post } from '@/api/types'
import { EmptyState, ErrorBox, PageHeader } from '@/components/common'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'

const money = (n: number) => n.toLocaleString(undefined, { style: 'currency', currency: 'INR', maximumFractionDigits: 0 })
const today = () => new Date().toISOString().slice(0, 10)

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <Card className="py-4">
      <CardContent className="px-5">
        <div className="text-xs text-muted-foreground">{label}</div>
        <div className="mt-1 text-2xl font-semibold tabular-nums">{value}</div>
      </CardContent>
    </Card>
  )
}

/** Views cell: edit inline, saves on blur. */
function ViewsCell({ post }: { post: Post }) {
  const qc = useQueryClient()
  const [v, setV] = useState(String(post.views))
  useEffect(() => setV(String(post.views)), [post.views])
  const upd = useMutation({
    mutationFn: (views: number) => api.updatePost(post.id, { views }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['posts'] }),
    onError: (e) => toast.error(e.message),
  })
  return (
    <Input
      className="h-8 w-28 text-right tabular-nums"
      inputMode="numeric"
      value={v}
      onChange={(e) => setV(e.target.value.replace(/[^\d]/g, ''))}
      onBlur={() => Number(v) !== post.views && upd.mutate(Number(v || 0))}
      onKeyDown={(e) => e.key === 'Enter' && (e.target as HTMLInputElement).blur()}
    />
  )
}

export default function Tracker() {
  const qc = useQueryClient()
  const [params, setParams] = useSearchParams()
  const { data, isLoading, error } = useQuery({ queryKey: ['posts'], queryFn: api.posts })
  const [open, setOpen] = useState(false)
  const empty: Partial<Post> = { platform: 'instagram', url: '', posted_at: today(), campaign: '', rate_per_1k: 0, views: 0, clip_title: '' }
  const [form, setForm] = useState<Partial<Post>>(empty)

  useEffect(() => {
    if (params.get('title')) {
      setForm({ ...empty, job_id: Number(params.get('job')), clip_rank: Number(params.get('rank')), clip_title: params.get('title') ?? '' })
      setOpen(true)
      setParams({}, { replace: true })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const create = useMutation({
    mutationFn: () => api.createPost(form),
    onSuccess: () => {
      toast.success('Post saved')
      setOpen(false)
      setForm(empty)
      qc.invalidateQueries({ queryKey: ['posts'] })
    },
    onError: (e) => toast.error(e.message),
  })
  const del = useMutation({
    mutationFn: (id: number) => api.deletePost(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['posts'] }),
  })
  const f = <K extends keyof Post>(k: K, v: Post[K]) => setForm((p) => ({ ...p, [k]: v }))

  return (
    <>
      <PageHeader
        title="Tracker"
        description="Log where you posted each clip and its views. Earnings = views ÷ 1000 × campaign rate."
        actions={
          <>
            <Button variant="outline" asChild>
              <a href="/api/posts.csv"><Download /> Export CSV</a>
            </Button>
            <Button onClick={() => { setForm(empty); setOpen(true) }}><Plus /> Add post</Button>
          </>
        }
      />
      {error ? <ErrorBox error={error} /> : isLoading ? <Skeleton className="h-64 rounded-xl" /> : (
        <>
          <div className="mb-6 grid grid-cols-3 gap-4">
            <Stat label="Posts" value={String(data!.totals.posts)} />
            <Stat label="Total views" value={data!.totals.views.toLocaleString()} />
            <Stat label="Est. earnings" value={money(data!.totals.earnings)} />
          </div>
          {!data!.posts.length ? (
            <EmptyState icon={BarChart3} title="Nothing tracked yet" action={<Button onClick={() => setOpen(true)}><Plus /> Add your first post</Button>}>
              After posting a clip on Instagram or YouTube, log it here and update views to see estimated campaign earnings.
            </EmptyState>
          ) : (
            <Card className="py-0">
              <div className="overflow-x-auto">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Clip</TableHead>
                      <TableHead>Platform</TableHead>
                      <TableHead>Posted</TableHead>
                      <TableHead>Campaign</TableHead>
                      <TableHead className="text-right">Rate / 1k</TableHead>
                      <TableHead className="text-right">Views</TableHead>
                      <TableHead className="text-right">Earnings</TableHead>
                      <TableHead />
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {data!.posts.map((p) => (
                      <TableRow key={p.id}>
                        <TableCell className="max-w-56">
                          <div className="truncate font-medium">{p.clip_title || '—'}</div>
                          {p.url && (
                            <a href={p.url} target="_blank" rel="noreferrer" className="flex items-center gap-1 truncate text-xs text-muted-foreground hover:text-foreground">
                              <ExternalLink className="size-3" /> {p.url.replace(/^https?:\/\//, '')}
                            </a>
                          )}
                        </TableCell>
                        <TableCell className="capitalize">{p.platform}</TableCell>
                        <TableCell className="tabular-nums">{p.posted_at ?? '—'}</TableCell>
                        <TableCell>{p.campaign || '—'}</TableCell>
                        <TableCell className="text-right tabular-nums">{money(p.rate_per_1k)}</TableCell>
                        <TableCell className="text-right"><div className="flex justify-end"><ViewsCell post={p} /></div></TableCell>
                        <TableCell className="text-right font-medium tabular-nums">{money(p.earnings)}</TableCell>
                        <TableCell>
                          <Button variant="ghost" size="icon" className="size-8" aria-label="Delete" onClick={() => confirm('Delete this post?') && del.mutate(p.id)}>
                            <Trash2 />
                          </Button>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            </Card>
          )}
        </>
      )}

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Log a posted clip</DialogTitle>
          </DialogHeader>
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-2 sm:col-span-2">
              <Label>Clip title</Label>
              <Input value={form.clip_title ?? ''} onChange={(e) => f('clip_title', e.target.value)} />
            </div>
            <div className="space-y-2">
              <Label>Platform</Label>
              <Select value={form.platform} onValueChange={(v) => f('platform', v as Post['platform'])}>
                <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                <SelectContent>
                  <SelectItem value="instagram">Instagram</SelectItem>
                  <SelectItem value="youtube">YouTube</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-2">
              <Label>Posted on</Label>
              <Input type="date" value={form.posted_at ?? ''} onChange={(e) => f('posted_at', e.target.value)} />
            </div>
            <div className="space-y-2 sm:col-span-2">
              <Label>Post URL</Label>
              <Input placeholder="https://instagram.com/reel/…" value={form.url ?? ''} onChange={(e) => f('url', e.target.value)} />
            </div>
            <div className="space-y-2">
              <Label>Campaign</Label>
              <Input value={form.campaign ?? ''} onChange={(e) => f('campaign', e.target.value)} />
            </div>
            <div className="space-y-2">
              <Label>Rate per 1,000 views (₹)</Label>
              <Input type="number" min={0} step="0.5" value={form.rate_per_1k ?? 0} onChange={(e) => f('rate_per_1k', Number(e.target.value))} />
            </div>
            <div className="space-y-2">
              <Label>Views so far</Label>
              <Input type="number" min={0} value={form.views ?? 0} onChange={(e) => f('views', Number(e.target.value))} />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setOpen(false)}>Cancel</Button>
            <Button onClick={() => create.mutate()} disabled={create.isPending}>Save</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
