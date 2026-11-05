import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import {
  Bot, CalendarClock, ExternalLink, Hand, FolderOpen, Link2, Loader2, PlugZap, RefreshCw, RotateCcw, Send, ShieldQuestion, Unplug,
} from 'lucide-react'
import { toast } from 'sonner'
import { pub } from '@/api/publish'
import type { FixAction, QueueItem } from '@/api/publishTypes'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

const LABEL: Partial<Record<FixAction, string>> = {
  sync: 'Sync with YouTube', link: 'Link to a video', open_batch: 'Open batch checklist', reschedule: 'Reschedule to next free slot',
  publish_now: 'Publish now', retry: 'Retry', reconnect: 'Reconnect', audit_info: 'What this means', studio: 'Open in YouTube Studio',
  unbatch: 'Upload with API instead', confirm: 'Confirm channel', open_app: 'Open the clip',
  switch_assisted: 'Switch account to assisted', switch_auto: 'Switch account to auto', studio_instead: 'Upload via Studio instead',
}
const ICON: Partial<Record<FixAction, typeof RefreshCw>> = {
  sync: RefreshCw, link: Link2, open_batch: FolderOpen, reschedule: CalendarClock, publish_now: Send, retry: RotateCcw,
  reconnect: PlugZap, audit_info: ShieldQuestion, studio: ExternalLink, unbatch: Unplug, confirm: PlugZap, open_app: ExternalLink,
  switch_assisted: Hand, switch_auto: Bot, studio_instead: FolderOpen,
}

/** Why a clip needs attention + one-click fixes. Never a bare "Needs you". */
export default function FixActions({ item, onLink, compact = false, onDone }: {
  item: QueueItem; onLink?: () => void; compact?: boolean; onDone?: () => void
}) {
  const qc = useQueryClient()
  const nav = useNavigate()
  const run = useMutation({
    mutationFn: async (a: FixAction) => {
      switch (a) {
        case 'sync': {
          const r = await pub.sync(item.channel_id)
          return `Linked ${r.matched.length} upload(s)`
        }
        case 'reschedule': {
          const r = await pub.reschedule(item.id)
          return `Moved to ${r.slot_local?.replace('T', ' ')}`
        }
        case 'retry':
          await pub.retry(item.id)
          return 'Queued again'
        case 'unbatch':
          await pub.unbatch(item.id)
          return 'Removed from the batch — API mode uploads it'
        case 'publish_now':
          await pub.publishNow(item.id)
          return 'Publishing now…'
        case 'switch_assisted':
          await pub.fix(item.id, a)
          return 'Account switched to assisted mode'
        case 'switch_auto':
          await pub.fix(item.id, a)
          return 'Account switched to auto mode'
        case 'studio_instead':
          await pub.fix(item.id, a)
          return 'Put in a Studio batch — upload it in YouTube Studio, then Sync'
        default:
          return ''
      }
    },
    onSuccess: (msg) => {
      if (msg) toast.success(msg)
      qc.invalidateQueries({ queryKey: ['queue'] })
      qc.invalidateQueries({ queryKey: ['health'] })
      onDone?.()
    },
    onError: (e) => toast.error(e.message),
  })
  const click = (a: FixAction) => {
    if (a === 'open_batch') {
      if (item.batch) window.open(`/api/batches/files/${item.batch}/batch.html`, '_blank')
      else nav('/batches')
    } else if (a === 'studio') {
      window.open(item.youtube_video_id ? `https://studio.youtube.com/video/${item.youtube_video_id}/edit` : 'https://studio.youtube.com', '_blank')
    } else if (a === 'reconnect' || a === 'confirm') nav('/channels')
    else if (a === 'audit_info') nav('/settings#audit')
    else if (a === 'link') onLink?.()
    else if (a === 'open_app') nav(`/jobs`)
    else if (a === 'publish_now') {
      if (window.confirm(`Publish “${item.title}” now${item.platform === 'youtube' ? ' (public on YouTube right away)' : ''}?`)) run.mutate(a)
    } else if (a === 'switch_auto') {
      if (window.confirm('Switch this account to auto mode? ClipForge will upload its scheduled clips by itself.')) run.mutate(a)
    } else run.mutate(a)
  }
  if (!item.reason && !item.error) return null
  const attention = ['failed', 'missed', 'needs_manual', 'locked_private'].includes(item.status) || !!item.reason
  return (
    <div className={cn('rounded-lg border p-3 text-sm', attention ? 'border-amber-500/30 bg-amber-500/10' : 'border-border bg-muted/30')}>
      {(item.status_text?.label || item.reason_label) && <div className="font-medium">{item.reason_label || item.status_text?.label}</div>}
      {item.error && !compact && <div className="mt-0.5 text-muted-foreground">{item.error}</div>}
      {item.actions.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-2">
          {item.actions.filter((a) => a !== 'link' || onLink).filter((a) => a !== 'edit').map((a) => {
            const Icon = ICON[a] ?? RefreshCw
            return (
              <Button key={a} size="sm" variant={a === item.actions[0] ? 'default' : 'outline'} disabled={run.isPending} onClick={() => click(a)}>
                {run.isPending && run.variables === a ? <Loader2 className="animate-spin" /> : <Icon />} {LABEL[a] ?? a}
              </Button>
            )
          })}
        </div>
      )}
    </div>
  )
}
