import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useMutation } from '@tanstack/react-query'
import { CalendarPlus, Check } from 'lucide-react'
import { toast } from 'sonner'
import { pub, useChannels } from '@/api/publish'
import { ChannelAvatar } from '@/components/ChannelPicker'
import PlatformIcon from '@/components/PlatformIcon'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from '@/components/ui/dialog'
import { cn } from '@/lib/utils'

/** "Add to schedule": queue approved clips (or given ranks) for one or more accounts — YouTube, Instagram or both.
 *  Titles/descriptions (YouTube) and captions (Instagram) are written in the background. */
export default function AddToQueueDialog({ jobId, ranks, label = 'Add to schedule', variant = 'outline', disabled }: {
  jobId: number
  ranks?: number[] | null
  label?: string
  variant?: 'outline' | 'default'
  disabled?: boolean
}) {
  const nav = useNavigate()
  const { data: channels = [] } = useChannels()
  const [open, setOpen] = useState(false)
  const [sel, setSel] = useState<string[]>([])
  const toggle = (id: string) => setSel((s) => (s.includes(id) ? s.filter((x) => x !== id) : [...s, id]))
  const add = useMutation({
    mutationFn: () => pub.add({ channel_ids: sel, job_id: jobId, ranks: ranks ?? null }),
    onSuccess: (r) => {
      setOpen(false)
      toast.success(r.added.length ? `${r.added.length} clip(s) added — writing titles & captions` : 'Already in those queues', {
        action: { label: 'Open schedule', onClick: () => nav('/schedule') },
      })
    },
    onError: (e) => toast.error(e.message),
  })
  const both = ['youtube', 'instagram'].every((p) => channels.some((c) => (c.platform ?? 'youtube') === p))
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant={variant} disabled={disabled}><CalendarPlus /> {label}</Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Add to which accounts?</DialogTitle>
          <DialogDescription>
            {ranks?.length ? `${ranks.length} clip(s)` : 'All approved clips of this job'} go to each account's drafts. Pick YouTube,
            Instagram or both; then click “Schedule approved clips” on the Schedule page.
          </DialogDescription>
        </DialogHeader>
        {!channels.length ? (
          <p className="text-sm">No accounts yet — <Link to="/channels" className="text-primary underline">connect one</Link>.</p>
        ) : (
          <div className="space-y-2">
            {both && (
              <Button size="sm" variant="ghost" onClick={() => setSel(channels.filter((c) => !c.paused).map((c) => c.id))}>
                Select all (YouTube + Instagram)
              </Button>
            )}
            {channels.map((c) => (
              <button key={c.id} type="button" onClick={() => toggle(c.id)} aria-pressed={sel.includes(c.id)}
                className={cn('flex w-full items-center gap-3 rounded-lg border p-3 text-left', sel.includes(c.id) ? 'border-primary bg-primary/5' : 'hover:border-primary/40')}>
                <ChannelAvatar ch={c} />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5 font-medium"><PlatformIcon platform={c.platform} /> {c.title}</div>
                  <div className="text-xs text-muted-foreground">{c.handle || c.id}{c.paused ? ' · paused' : ''}</div>
                </div>
                {sel.includes(c.id) && <Check className="size-4 text-primary" />}
              </button>
            ))}
          </div>
        )}
        <DialogFooter>
          <Button onClick={() => add.mutate()} disabled={!sel.length || add.isPending}>Add to {sel.length || ''} account{sel.length === 1 ? '' : 's'}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
