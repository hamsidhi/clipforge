import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ChevronDown, ExternalLink, History as HistoryIcon } from 'lucide-react'
import { pub, type HistoryDay } from '@/api/publish'
import { EmptyState, ErrorBox, PageHeader } from '@/components/common'
import PlatformIcon from '@/components/PlatformIcon'
import { Skeleton } from '@/components/ui/skeleton'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'

const dayLabel = (iso: string) =>
  new Date(`${iso}T12:00:00`).toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' })

function Day({ d }: { d: HistoryDay }) {
  const [open, setOpen] = useState(false)
  return (
    <section className="overflow-hidden rounded-xl border">
      <header className="flex flex-wrap items-baseline justify-between gap-2 bg-muted/40 px-4 py-3">
        <h2 className="font-medium">{dayLabel(d.date)}</h2>
        <span className="text-sm text-muted-foreground">
          {d.published} published · {d.views.toLocaleString()} views
        </span>
      </header>
      {d.posts.length > 0 && (
        <ul className="divide-y">
          {d.posts.map((p, i) => (
            <li key={i} className="flex items-center gap-3 px-4 py-2.5 text-sm">
              <span className="w-11 shrink-0 tabular-nums text-muted-foreground">{p.time}</span>
              <PlatformIcon platform={p.platform} />
              <span className="min-w-0 flex-1 truncate">{p.title || '(no title)'}</span>
              {p.views !== null && <span className="shrink-0 tabular-nums text-muted-foreground">{p.views.toLocaleString()} views</span>}
              {p.link && (
                <a href={p.link} target="_blank" rel="noreferrer" className="shrink-0 text-muted-foreground hover:text-foreground" aria-label="Open post">
                  <ExternalLink className="size-4" />
                </a>
              )}
            </li>
          ))}
        </ul>
      )}
      {d.events.length > 0 && (
        <div className="border-t">
          <button className="flex w-full items-center gap-2 px-4 py-2 text-left text-xs text-muted-foreground hover:bg-muted/30"
            onClick={() => setOpen(!open)}>
            <ChevronDown className={`size-4 transition-transform ${open ? 'rotate-180' : ''}`} />
            Activity log ({d.events.length} change{d.events.length === 1 ? '' : 's'})
          </button>
          {open && (
            <ul className="divide-y border-t text-xs">
              {d.events.map((e, i) => (
                <li key={i} className="flex gap-3 px-4 py-2">
                  <span className="w-11 shrink-0 tabular-nums text-muted-foreground">{e.time}</span>
                  <PlatformIcon platform={e.platform} />
                  <div className="min-w-0 flex-1">
                    <div><span className="font-medium">{e.label}</span> · <span className="text-muted-foreground">{e.title || '(no title)'}</span></div>
                    {(e.detail || e.slot) && (
                      <div className="truncate text-muted-foreground">
                        {e.slot && `slot ${e.slot} IST`}{e.slot && e.detail ? ' · ' : ''}{e.detail}
                      </div>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  )
}

/** Every post and every change to the schedule, kept forever (publish/history.py). */
export default function History() {
  const [platform, setPlatform] = useState('all')
  const [range, setRange] = useState('30')
  const { data, isLoading, error } = useQuery({
    queryKey: ['history', platform, range], queryFn: () => pub.history(Number(range), platform), refetchInterval: 120000,
  })
  const days = (data?.days ?? []).filter((d) => d.posts.length || d.events.length)
  const total = days.reduce((a, d) => a + d.published, 0)
  return (
    <>
      <PageHeader title="History" description={data ? `${total} posts published in the last ${range} days` : 'What was posted each day, and every change'}
        actions={
          <div className="flex flex-wrap gap-2">
            <ToggleGroup type="single" variant="outline" value={platform} onValueChange={(v) => v && setPlatform(v)}>
              <ToggleGroupItem value="all">All</ToggleGroupItem>
              <ToggleGroupItem value="youtube">YouTube</ToggleGroupItem>
              <ToggleGroupItem value="instagram">Instagram</ToggleGroupItem>
            </ToggleGroup>
            <ToggleGroup type="single" variant="outline" value={range} onValueChange={(v) => v && setRange(v)}>
              <ToggleGroupItem value="7">7 d</ToggleGroupItem>
              <ToggleGroupItem value="30">30 d</ToggleGroupItem>
              <ToggleGroupItem value="365">All</ToggleGroupItem>
            </ToggleGroup>
          </div>
        } />
      {error ? <ErrorBox error={error} /> : isLoading ? (
        <div className="space-y-3">{[0, 1, 2].map((i) => <Skeleton key={i} className="h-32 rounded-xl" />)}</div>
      ) : days.length === 0 ? (
        <EmptyState icon={HistoryIcon} title="No history yet">Posts and schedule changes show up here.</EmptyState>
      ) : (
        <div className="space-y-4">{days.map((d) => <Day key={d.date} d={d} />)}</div>
      )}
    </>
  )
}
