import { Check, CircleDashed, Loader2, SkipForward, X } from 'lucide-react'
import { formatDuration, STAGE_LABELS, STAGES, type StageState } from '@/api/hooks'
import { Progress } from '@/components/ui/progress'
import { cn } from '@/lib/utils'

function Icon({ s }: { s: StageState['status'] }) {
  if (s === 'done') return <Check className="size-3.5" strokeWidth={3} />
  if (s === 'skipped') return <SkipForward className="size-3.5" />
  if (s === 'running') return <Loader2 className="size-3.5 animate-spin" />
  if (s === 'error') return <X className="size-3.5" strokeWidth={3} />
  return <CircleDashed className="size-3.5" />
}

export default function ProgressStepper({ stages, failed }: { stages: Record<string, StageState>; failed?: boolean }) {
  return (
    <ol className="space-y-1">
      {STAGES.map((name, i) => {
        const st = stages[name] ?? { status: 'pending', progress: 0, eta: null, message: '' }
        const status = failed && st.status === 'running' ? 'error' : st.status
        const active = status === 'running'
        return (
          <li key={name} className={cn('relative flex gap-4 rounded-xl px-3 py-3 transition-colors', active && 'bg-primary/5')}>
            {i < STAGES.length - 1 && <span className="absolute top-10 bottom-[-6px] left-[23px] w-px bg-border" />}
            <span
              className={cn(
                'z-10 grid size-6 shrink-0 place-items-center rounded-full border',
                status === 'done' && 'border-success bg-success text-black',
                status === 'skipped' && 'border-success/50 bg-success/15 text-success',
                status === 'running' && 'border-primary bg-primary text-primary-foreground',
                status === 'error' && 'border-destructive bg-destructive text-white',
                status === 'pending' && 'text-muted-foreground',
              )}
            >
              <Icon s={status} />
            </span>
            <div className="min-w-0 flex-1">
              <div className="flex items-baseline justify-between gap-2">
                <span className={cn('text-sm font-medium', status === 'pending' && 'text-muted-foreground')}>{STAGE_LABELS[name]}</span>
                <span className="shrink-0 text-xs text-muted-foreground tabular-nums">
                  {active ? `${Math.round(st.progress * 100)}%${st.eta != null ? ` · ${formatDuration(st.eta)} left` : ''}` : status === 'skipped' ? 'cached' : ''}
                </span>
              </div>
              {active && <Progress value={st.progress * 100} className="mt-2 h-1.5" />}
              {st.message && status !== 'pending' && <div className="mt-1 truncate text-xs text-muted-foreground">{st.message}</div>}
            </div>
          </li>
        )
      })}
    </ol>
  )
}
