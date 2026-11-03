import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useMutation } from '@tanstack/react-query'
import { CalendarPlus } from 'lucide-react'
import { toast } from 'sonner'
import { pub, useChannels } from '@/api/publish'
import { ChannelAvatar } from '@/components/ChannelPicker'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from '@/components/ui/dialog'
import { cn } from '@/lib/utils'

/** "Add to schedule": queue approved clips (or given ranks) for a channel; metadata is written in the background. */
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
  const [channel, setChannel] = useState<string>()
  const add = useMutation({
    mutationFn: () => pub.add({ channel_id: channel!, job_id: jobId, ranks: ranks ?? null }),
    onSuccess: (r) => {
      setOpen(false)
      toast.success(r.added.length ? `${r.added.length} clip(s) added — writing titles & descriptions` : 'Already in this channel\'s queue', {
        action: { label: 'Open schedule', onClick: () => nav('/schedule') },
      })
    },
    onError: (e) => toast.error(e.message),
  })
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant={variant} disabled={disabled}><CalendarPlus /> {label}</Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Add to which channel?</DialogTitle>
          <DialogDescription>
            {ranks?.length ? `${ranks.length} clip(s)` : 'All approved clips of this job'} go to the channel's drafts. ClipForge writes YouTube
            titles, descriptions and tags; then click “Schedule approved clips” on the Schedule page.
          </DialogDescription>
        </DialogHeader>
        {!channels.length ? (
          <p className="text-sm">No channels yet — <Link to="/channels" className="text-primary underline">connect one</Link>.</p>
        ) : (
          <div className="space-y-2">
            {channels.map((c) => (
              <button key={c.id} type="button" onClick={() => setChannel(c.id)}
                className={cn('flex w-full items-center gap-3 rounded-lg border p-3 text-left', channel === c.id ? 'border-primary bg-primary/5' : 'hover:border-primary/40')}>
                <ChannelAvatar ch={c} />
                <div className="min-w-0 flex-1">
                  <div className="font-medium">{c.title}</div>
                  <div className="text-xs text-muted-foreground">{c.handle || c.id}{c.paused ? ' · paused' : ''}</div>
                </div>
              </button>
            ))}
          </div>
        )}
        <DialogFooter>
          <Button onClick={() => add.mutate()} disabled={!channel || add.isPending}>Add</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
