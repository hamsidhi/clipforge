import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CheckCircle2, CircleAlert, Download, Loader2, MinusCircle, RefreshCw } from 'lucide-react'
import { toast } from 'sonner'
import { api } from '@/api/client'
import { CopyButton, ErrorBox, PageHeader } from '@/components/common'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'

export default function SystemCheck() {
  const qc = useQueryClient()
  const { data, isLoading, isFetching, error, refetch } = useQuery({ queryKey: ['doctor'], queryFn: api.doctor })
  const fix = useMutation({
    mutationFn: api.doctorFix,
    onSuccess: (d) => {
      qc.setQueryData(['doctor'], d)
      toast.success('Downloaded missing fonts & models')
    },
    onError: (e) => toast.error(e.message),
  })
  const failed = (data ?? []).filter((c) => !c.ok && c.required).length
  return (
    <>
      <PageHeader
        title="System check"
        description="The same checks as `clipforge doctor`."
        actions={
          <>
            <Button variant="outline" onClick={() => fix.mutate()} disabled={fix.isPending}>
              {fix.isPending ? <Loader2 className="animate-spin" /> : <Download />} Download fonts &amp; models
            </Button>
            <Button onClick={() => refetch()} disabled={isFetching}>
              <RefreshCw className={isFetching ? 'animate-spin' : ''} /> Re-check
            </Button>
          </>
        }
      />
      {error ? <ErrorBox error={error} /> : isLoading ? <Skeleton className="h-96 rounded-xl" /> : (
        <>
          <div className={`mb-5 rounded-xl border p-4 text-sm ${failed ? 'border-destructive/40 bg-destructive/10' : 'border-success/40 bg-success/10'}`}>
            {failed ? `${failed} required check(s) need attention — follow the fix next to each one.` : 'All required checks passed. You are ready to make clips.'}
          </div>
          <Card className="divide-y py-0">
            {data!.map((c) => (
              <div key={c.name} className="flex flex-col gap-3 p-4 sm:flex-row sm:items-center">
                <div className="flex min-w-0 flex-1 items-start gap-3">
                  {c.ok ? <CheckCircle2 className="mt-0.5 size-5 shrink-0 text-success" /> : c.required ? <CircleAlert className="mt-0.5 size-5 shrink-0 text-destructive" /> : <MinusCircle className="mt-0.5 size-5 shrink-0 text-muted-foreground" />}
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2 text-sm font-medium">
                      {c.name}
                      {!c.required && <Badge variant="outline" className="text-[10px]">optional</Badge>}
                    </div>
                    <div className="mt-0.5 text-xs break-words text-muted-foreground">{c.detail}</div>
                  </div>
                </div>
                {!c.ok && c.fix && (
                  <div className="flex items-center gap-2 sm:max-w-[50%]">
                    <code className="min-w-0 flex-1 rounded-md bg-muted px-2 py-1.5 font-mono text-[11px] break-words">{c.fix}</code>
                    <CopyButton text={c.fix} label="" />
                  </div>
                )}
              </div>
            ))}
          </Card>
        </>
      )}
    </>
  )
}
