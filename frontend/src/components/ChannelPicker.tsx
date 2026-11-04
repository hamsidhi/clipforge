import { useEffect } from 'react'
import { Layers } from 'lucide-react'
import { useChannels } from '@/api/publish'
import type { Channel, Platform } from '@/api/publishTypes'
import PlatformIcon from '@/components/PlatformIcon'
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

/** Account selector (YouTube channels + Instagram accounts), remembered across pages (localStorage).
 *  `allowAll` adds "All accounts" (value 'all'); `platform` limits the list. */
export default function ChannelPicker({ value, onChange, allowAll = false, platform }: {
  value: string | undefined; onChange: (id: string) => void; allowAll?: boolean; platform?: Platform
}) {
  const { data = [] } = useChannels()
  const channels = platform ? data.filter((c) => (c.platform ?? 'youtube') === platform) : data
  useEffect(() => {
    if (value && (value === 'all' ? allowAll : channels.some((c) => c.id === value))) return
    if (!channels.length) return
    let saved: string | null = null
    try {
      saved = localStorage.getItem(KEY)
    } catch {
      /* private mode */
    }
    if (saved === 'all' && allowAll) onChange('all')
    else onChange(channels.find((c) => c.id === saved)?.id ?? channels[0].id)
  }, [channels, value, onChange, allowAll])
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
        <SelectValue placeholder="Choose an account" />
      </SelectTrigger>
      <SelectContent>
        {allowAll && channels.length > 1 && (
          <SelectItem value="all"><span className="flex items-center gap-2"><Layers className="size-4" /> All accounts</span></SelectItem>
        )}
        {channels.map((c) => (
          <SelectItem key={c.id} value={c.id}>
            <span className="flex items-center gap-2">
              <ChannelAvatar ch={c} className="size-5" />
              <PlatformIcon platform={c.platform} />
              {c.title}
              {c.paused && <span className="text-xs text-muted-foreground">(paused)</span>}
            </span>
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}
