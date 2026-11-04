import { useEffect, useRef, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ExternalLink, Link2, Loader2, RefreshCw, Save, Trash2, Undo2 } from 'lucide-react'
import { toast } from 'sonner'
import { pub, STATUS_LABEL, STATUS_STYLE, useChannels } from '@/api/publish'
import type { Privacy, QueueItem } from '@/api/publishTypes'
import { CopyButton } from '@/components/common'
import FixActions from '@/components/FixActions'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Textarea } from '@/components/ui/textarea'
import { cn } from '@/lib/utils'

function Counter({ n, max, hint }: { n: number; max: number; hint?: number }) {
  return (
    <span className={cn('text-xs tabular-nums', n > max ? 'text-destructive' : hint && n > hint ? 'text-amber-300' : 'text-muted-foreground')}
      title={hint ? `YouTube allows ${max}; under ${hint} reads best on Shorts` : undefined}>{n}/{max}</span>
  )
}

const PRIVACY: { value: Privacy; label: string; help: string }[] = [
  { value: 'scheduled', label: 'Scheduled (recommended)', help: 'Uploaded early as private; YouTube makes it public at the slot, even if this laptop is off.' },
  { value: 'public', label: 'Public at the slot', help: 'Uploaded and made public exactly at the slot — the laptop must be on then.' },
  { value: 'unlisted', label: 'Unlisted', help: 'Uploaded now; only people with the link can watch. Never goes public by itself.' },
  { value: 'private', label: 'Private', help: 'Uploaded now; only you can watch. Never goes public by itself.' },
]

export default function QueueItemDialog({ item, onClose }: { item: QueueItem | null; onClose: () => void }) {
  const qc = useQueryClient()
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [tags, setTags] = useState('')
  const [slot, setSlot] = useState('')
  const [ig, setIg] = useState('')
  const [linkId, setLinkId] = useState('')
  const [privacy, setPrivacy] = useState<Privacy>('scheduled')
  const linkRef = useRef<HTMLInputElement>(null)
  const { data: channels = [] } = useChannels()
  const channel = channels.find((c) => c.id === item?.channel_id)
  useEffect(() => {
    if (!item) return
    setPrivacy(item.privacy ?? 'scheduled')
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
  const locked = !!item && (['uploading', 'ready', 'scheduled', 'uploaded', 'published', 'locked_private'].includes(item.status) || !!item.youtube_video_id || !!item.ig_media_id)
  const yt = item?.platform !== 'instagram'
  const inBatch = !!item?.batch
  const save = useMutation({
    mutationFn: () => {
      const body: Parameters<typeof pub.patch>[1] = {
        title, description, ig_caption: ig, tags: tags.split(',').map((t) => t.trim()).filter(Boolean), privacy,
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
      <DialogContent className="max-h-[90dvh] grid-cols-[minmax(0,1fr)] overflow-y-auto overflow-x-hidden sm:max-w-2xl">
        {item && (
          <>
            <DialogHeader>
              <DialogTitle className="flex flex-wrap items-center gap-2 pr-6">
                <Badge variant="outline" className={cn('border-transparent', STATUS_STYLE[item.status])}>{STATUS_LABEL[item.status]}</Badge>
                <span className="truncate text-sm font-normal text-muted-foreground">{item.episode_title} · #{item.rank} · {Math.round(item.duration)}s</span>
              </DialogTitle>
            </DialogHeader>
            {channel && (
              <div className="flex items-center gap-2 rounded-lg border bg-muted/30 p-2 text-sm">
                {channel.thumbnail && <img src={channel.thumbnail} alt="" className="size-7 rounded-full" />}
                <span>{yt ? 'Uploads to YouTube channel' : 'Posts to Instagram account'} <b>{channel.title}</b>{channel.handle ? ` (${channel.handle})` : ''}</span>
              </div>
            )}
            <FixActions item={item} onLink={() => linkRef.current?.focus()} onDone={onClose} />
            {!item.reason && item.error && <div className="rounded-lg border bg-muted/30 p-3 text-sm text-muted-foreground">{item.error}</div>}
            {item.yt_state?.checked_at && (
              <div className="text-xs text-muted-foreground">
                On YouTube: {item.yt_state.uploadStatus ?? '—'} · {item.yt_state.privacyStatus ?? '—'}
                {item.yt_state.publishAt ? ` · publishes ${new Date(item.yt_state.publishAt).toLocaleString('en-IN')}` : ''}
                {item.yt_state.processingStatus ? ` · processing ${item.yt_state.processingStatus}` : ''} (checked {new Date(item.yt_state.checked_at).toLocaleTimeString('en-IN')})
              </div>
            )}
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
                  <div className="flex justify-between"><Label>Title</Label><Counter n={title.length} max={100} hint={70} /></div>
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
                  {yt && (
                    <div className="space-y-1.5">
                      <Label>Visibility</Label>
                      <Select value={privacy} onValueChange={(v) => setPrivacy(v as Privacy)} disabled={locked}>
                        <SelectTrigger className="w-full"><SelectValue /></SelectTrigger>
                        <SelectContent>{PRIVACY.map((p) => <SelectItem key={p.value} value={p.value}>{p.label}</SelectItem>)}</SelectContent>
                      </Select>
                      <p className="text-xs text-muted-foreground">{PRIVACY.find((p) => p.value === privacy)?.help}</p>
                    </div>
                  )}
                  <div className="space-y-1.5 sm:col-span-2">
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
            {yt && (
              <p className="text-xs text-muted-foreground">
                By uploading you agree to the <a className="underline" href="https://www.youtube.com/t/terms" target="_blank" rel="noreferrer">YouTube Terms of Service</a>.
                ClipForge uses YouTube API Services; see <a className="underline" href="https://policies.google.com/privacy" target="_blank" rel="noreferrer">Google's Privacy Policy</a>.
                Disconnect (revoke access) any time on the Accounts page.
              </p>
            )}
            {yt && !item.youtube_video_id && ['queued', 'needs_manual', 'missed', 'failed'].includes(item.status) && (
              <div className="flex gap-2">
                <Input ref={linkRef} placeholder="Uploaded already? Paste the YouTube link/id to link it" value={linkId} onChange={(e) => setLinkId(e.target.value)} />
                <Button variant="outline" disabled={!linkId || act.isPending} onClick={() => act.mutate('link')}><Link2 /> Link</Button>
              </div>
            )}
            <div className="flex flex-wrap gap-2 pt-2">
              <Button onClick={() => save.mutate()} disabled={locked || save.isPending || item.meta_status === 'pending'}><Save /> Save</Button>
              <Button variant="outline" onClick={() => act.mutate('regen')} disabled={locked || inBatch || act.isPending} title={inBatch ? 'In a prepared Studio batch — unschedule first' : undefined}><RefreshCw /> Regenerate</Button>
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
