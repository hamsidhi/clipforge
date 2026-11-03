import { useEffect, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ExternalLink, Link2, Loader2, RefreshCw, Save, Trash2, Undo2 } from 'lucide-react'
import { toast } from 'sonner'
import { pub, STATUS_LABEL, STATUS_STYLE } from '@/api/publish'
import type { QueueItem } from '@/api/publishTypes'
import { CopyButton } from '@/components/common'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { cn } from '@/lib/utils'

function Counter({ n, max }: { n: number; max: number }) {
  return <span className={cn('text-xs tabular-nums', n > max ? 'text-destructive' : 'text-muted-foreground')}>{n}/{max}</span>
}

export default function QueueItemDialog({ item, onClose }: { item: QueueItem | null; onClose: () => void }) {
  const qc = useQueryClient()
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [tags, setTags] = useState('')
  const [slot, setSlot] = useState('')
  const [ig, setIg] = useState('')
  const [linkId, setLinkId] = useState('')
  useEffect(() => {
    if (!item) return
    setTitle(item.title)
    setDescription(item.description)
    setTags(item.tags.join(', '))
    setSlot(item.slot_local ?? '')
    setIg(item.ig_caption)
  }, [item])
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ['queue'] })
    qc.invalidateQueries({ queryKey: ['channels'] })
  }
  const locked = !!item && ['uploading', 'scheduled', 'published'].includes(item.status)
  const save = useMutation({
    mutationFn: () => {
      const body: Parameters<typeof pub.patch>[1] = {
        title, description, ig_caption: ig, tags: tags.split(',').map((t) => t.trim()).filter(Boolean),
      }
      if (slot && slot !== item!.slot_local) body.slot_local = slot
      return pub.patch(item!.id, body)
    },
    onSuccess: () => {
      toast.success('Saved')
      refresh()
      onClose()
    },
    onError: (e) => toast.error(e.message),
  })
  const act = useMutation({
    mutationFn: async (a: 'regen' | 'unschedule' | 'delete' | 'link') => {
      if (a === 'regen') return pub.regenerate(item!.id)
      if (a === 'unschedule') return pub.unschedule(item!.id)
      if (a === 'link') return pub.link(item!.id, linkId)
      return pub.remove(item!.id)
    },
    onSuccess: (_, a) => {
      toast.success(a === 'regen' ? 'Writing new metadata…' : a === 'delete' ? 'Removed from queue' : a === 'link' ? 'Linked' : 'Unscheduled')
      refresh()
      onClose()
    },
    onError: (e) => toast.error(e.message),
  })
  const tagChars = tags.split(',').map((t) => t.trim()).filter(Boolean).reduce((s, t) => s + t.length + (t.includes(' ') ? 2 : 0) + 1, -1)
  return (
    <Dialog open={!!item} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-2xl">
        {item && (
          <>
            <DialogHeader>
              <DialogTitle className="flex flex-wrap items-center gap-2 pr-6">
                <Badge variant="outline" className={cn('border-transparent', STATUS_STYLE[item.status])}>{STATUS_LABEL[item.status]}</Badge>
                <span className="truncate text-sm font-normal text-muted-foreground">{item.episode_title} · #{item.rank} · {Math.round(item.duration)}s</span>
              </DialogTitle>
            </DialogHeader>
            {item.error && <div className="rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-sm">{item.error}</div>}
            {item.problems.length > 0 && (
              <ul className="list-disc rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 pl-6 text-sm text-amber-200">
                {item.problems.map((p) => <li key={p}>{p}</li>)}
              </ul>
            )}
            {item.meta_status === 'pending' ? (
              <div className="flex items-center gap-2 py-6 text-sm text-muted-foreground"><Loader2 className="size-4 animate-spin" /> Writing title, description and tags…</div>
            ) : (
              <div className="space-y-4">
                <div className="space-y-1.5">
                  <div className="flex justify-between"><Label>Title</Label><Counter n={title.length} max={70} /></div>
                  <Input value={title} disabled={locked} onChange={(e) => setTitle(e.target.value)} />
                  {item.variants.length > 1 && (
                    <div className="flex flex-wrap gap-1.5 pt-1">
                      {item.variants.map((v) => (
                        <button key={v.title} type="button" disabled={locked || v.duplicate} onClick={() => setTitle(v.title)}
                          title={v.reasons.join(', ')}
                          className={cn('rounded-md border px-2 py-1 text-left text-xs hover:border-primary/50', v.title === title && 'border-primary',
                            v.duplicate && 'line-through opacity-50')}>
                          <span className="mr-1 font-semibold text-primary">{v.score}</span>{v.title}
                        </button>
                      ))}
                    </div>
                  )}
                </div>
                <div className="space-y-1.5">
                  <div className="flex justify-between"><Label>Description</Label><Counter n={description.length} max={5000} /></div>
                  <Textarea rows={7} value={description} disabled={locked} onChange={(e) => setDescription(e.target.value)} />
                </div>
                <div className="space-y-1.5">
                  <div className="flex justify-between"><Label>Tags (comma-separated)</Label><Counter n={Math.max(0, tagChars)} max={500} /></div>
                  <Textarea rows={2} value={tags} disabled={locked} onChange={(e) => setTags(e.target.value)} />
                </div>
                <div className="grid gap-4 sm:grid-cols-2">
                  <div className="space-y-1.5">
                    <Label>Publish time (IST)</Label>
                    <Input type="datetime-local" value={slot} disabled={locked} onChange={(e) => setSlot(e.target.value)} />
                  </div>
                  <div className="space-y-1.5">
                    <Label>Instagram caption</Label>
                    <div className="flex gap-2"><Input value={ig} onChange={(e) => setIg(e.target.value)} /><CopyButton text={ig} label="" /></div>
                  </div>
                </div>
              </div>
            )}
            {item.youtube_url && (
              <a href={item.youtube_url} target="_blank" rel="noreferrer" className="flex items-center gap-1 text-sm text-primary">
                <ExternalLink className="size-3.5" /> {item.youtube_url}
              </a>
            )}
            {!item.youtube_video_id && ['queued', 'needs_manual'].includes(item.status) && (
              <div className="flex gap-2">
                <Input placeholder="Uploaded already? Paste the YouTube link/id to link it" value={linkId} onChange={(e) => setLinkId(e.target.value)} />
                <Button variant="outline" disabled={!linkId || act.isPending} onClick={() => act.mutate('link')}><Link2 /> Link</Button>
              </div>
            )}
            <div className="flex flex-wrap gap-2 pt-2">
              <Button onClick={() => save.mutate()} disabled={locked || save.isPending || item.meta_status === 'pending'}><Save /> Save</Button>
              <Button variant="outline" onClick={() => act.mutate('regen')} disabled={locked || act.isPending}><RefreshCw /> Regenerate</Button>
              {item.slot_utc && !locked && <Button variant="outline" onClick={() => act.mutate('unschedule')}><Undo2 /> Unschedule</Button>}
              {!locked && (
                <Button variant="ghost" className="ml-auto text-destructive" onClick={() => window.confirm('Remove this clip from the queue?') && act.mutate('delete')}>
                  <Trash2 /> Remove
                </Button>
              )}
            </div>
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}
