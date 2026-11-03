import { useState, type ReactNode } from 'react'
import { Check, Copy, type LucideIcon } from 'lucide-react'
import { toast } from 'sonner'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import type { JobStatus } from '@/api/types'

export function PageHeader({ title, description, actions }: { title: ReactNode; description?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="mb-6 flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
      <div className="min-w-0">
        <h1 className="truncate text-2xl font-semibold tracking-tight">{title}</h1>
        {description && <p className="mt-1 text-sm text-muted-foreground">{description}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  )
}

export function EmptyState({ icon: Icon, title, children, action }: { icon: LucideIcon; title: string; children?: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center rounded-2xl border border-dashed px-6 py-16 text-center">
      <div className="mb-4 grid size-12 place-items-center rounded-full bg-muted">
        <Icon className="size-5 text-muted-foreground" />
      </div>
      <h3 className="font-medium">{title}</h3>
      {children && <div className="mt-1.5 max-w-sm text-sm text-muted-foreground">{children}</div>}
      {action && <div className="mt-5">{action}</div>}
    </div>
  )
}

export function ScoreBadge({ score, className }: { score: number; className?: string }) {
  const tone = score >= 7.5 ? 'bg-primary text-primary-foreground' : score >= 6 ? 'bg-secondary text-foreground' : 'bg-muted text-muted-foreground'
  return <span className={cn('inline-flex items-center rounded-md px-1.5 py-0.5 text-xs font-semibold tabular-nums', tone, className)}>{score.toFixed(1)}</span>
}

const STATUS_STYLE: Record<JobStatus, string> = {
  queued: 'bg-muted text-muted-foreground',
  running: 'bg-primary/15 text-primary',
  done: 'bg-success/15 text-success',
  failed: 'bg-destructive/15 text-destructive',
  cancelled: 'bg-muted text-muted-foreground',
}

export function StatusBadge({ status }: { status: JobStatus }) {
  return (
    <Badge variant="outline" className={cn('border-transparent capitalize', STATUS_STYLE[status])}>
      {status === 'running' && <span className="mr-1 size-1.5 animate-pulse rounded-full bg-primary" />}
      {status}
    </Badge>
  )
}

export function CopyButton({ text, label = 'Copy', className }: { text: string; label?: string; className?: string }) {
  const [done, setDone] = useState(false)
  return (
    <Button
      size="sm"
      variant={done ? 'secondary' : 'outline'}
      className={className}
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(text)
        } catch {
          // http://<lan-ip> is not a secure context: fall back to a hidden textarea
          const ta = document.createElement('textarea')
          ta.value = text
          document.body.appendChild(ta)
          ta.select()
          document.execCommand('copy')
          ta.remove()
        }
        setDone(true)
        toast.success('Copied to clipboard')
        setTimeout(() => setDone(false), 1500)
      }}
    >
      {done ? <Check /> : <Copy />}
      {label}
    </Button>
  )
}

export function ErrorBox({ error, action }: { error: unknown; action?: ReactNode }) {
  const msg = error instanceof Error ? error.message : String(error)
  return (
    <div className="rounded-xl border border-destructive/30 bg-destructive/10 p-4 text-sm">
      <div className="font-medium text-destructive">Something went wrong</div>
      <div className="mt-1 whitespace-pre-wrap text-muted-foreground">{msg}</div>
      {action && <div className="mt-3">{action}</div>}
    </div>
  )
}
