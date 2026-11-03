import { useEffect } from 'react'
import { useChannels } from '@/api/publish'
import type { Channel } from '@/api/publishTypes'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { cn } from '@/lib/utils'

const KEY = 'cf-channel'

export function ChannelAvatar({ ch, className }: { ch: Pick<Channel, 'thumbnail' | 'title'>; className?: string }) {
  return ch.thumbnail ? (
    <img src={ch.thumbnail} alt="" className={cn('size-7 shrink-0 rounded-full object-cover', className)} />
  ) : (
    <div className={cn('grid size-7 shrink-0 place-items-center rounded-full bg-primary text-xs font-bold text-primary-foreground', className)}>
      {ch.title.slice(0, 1).toUpperCase() || '?'}
    </div>
  )
}

/** Channel selector remembered across pages (localStorage). */
export default function ChannelPicker({ value, onChange }: { value: string | undefined; onChange: (id: string) => void }) {
  const { data: channels = [] } = useChannels()
  useEffect(() => {
    if (value || !channels.length) return
    let saved: string | null = null
    try {
      saved = localStorage.getItem(KEY)
    } catch {
      /* private mode */
    }
    onChange(channels.find((c) => c.id === saved)?.id ?? channels[0].id)
  }, [channels, value, onChange])
  if (!channels.length) return null
  return (
    <Select
      value={value}
      onValueChange={(v) => {
        try {
          localStorage.setItem(KEY, v)
        } catch {
          /* ignore */
        }
        onChange(v)
      }}
    >
      <SelectTrigger className="w-64">
        <SelectValue placeholder="Choose a channel" />
      </SelectTrigger>
      <SelectContent>
        {channels.map((c) => (
          <SelectItem key={c.id} value={c.id}>
            <span className="flex items-center gap-2">
              <ChannelAvatar ch={c} className="size-5" />
              {c.title}
              {c.paused && <span className="text-xs text-muted-foreground">(paused)</span>}
            </span>
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}
