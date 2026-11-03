import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ExternalLink, FolderUp, PackageOpen } from 'lucide-react'
import { toast } from 'sonner'
import { pub, usePublishing, useQueue } from '@/api/publish'
import ChannelPicker from '@/components/ChannelPicker'
import { CopyButton, EmptyState, PageHeader } from '@/components/common'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'

export default function Batches() {
  const qc = useQueryClient()
  const [channelId, setChannelId] = useState<string>()
  const [n, setN] = useState(15)
  const { data: status } = usePublishing()
  const { data: batches = [] } = useQuery({ queryKey: ['batches'], queryFn: pub.batches })
  const { data: items = [] } = useQueue(channelId)
  const waiting = items.filter((i) => i.status === 'queued' && i.slot_utc && !i.batch)
  const prep = useMutation({
    mutationFn: () => pub.prepareBatch(channelId!, n),
    onSuccess: (b) => {
      toast.success(`Batch ready: ${b.count} clips`)
      qc.invalidateQueries({ queryKey: ['batches'] })
      qc.invalidateQueries({ queryKey: ['queue', channelId] })
      window.open(b.url, '_blank')
    },
    onError: (e) => toast.error(e.message),
  })
  const max = 15
  return (
    <>
      <PageHeader
        title="Upload batches"
        description="Assisted mode: ClipForge prepares the files and a checklist, you upload them in YouTube Studio (official site — always allowed)."
        actions={<ChannelPicker value={channelId} onChange={setChannelId} />}
      />
      <Card className="mb-6">
        <CardHeader><CardTitle className="text-base">Prepare the next batch</CardTitle></CardHeader>
        <CardContent className="flex flex-wrap items-end gap-3 text-sm">
          <div className="space-y-1">
            <div className="text-xs text-muted-foreground">Clips (max {max} per Studio upload)</div>
            <Input type="number" min={1} max={max} value={n} onChange={(e) => setN(Math.min(max, Math.max(1, Number(e.target.value))))} className="w-28" />
          </div>
          <Button onClick={() => prep.mutate()} disabled={!channelId || !waiting.length || prep.isPending}><FolderUp /> Prepare batch</Button>
          <span className="text-muted-foreground">{waiting.length} scheduled clip(s) waiting for a batch</span>
          {status?.mode === 'api' && <span className="text-xs text-amber-300">You're in API mode — ClipForge uploads by itself; batches are optional.</span>}
        </CardContent>
      </Card>
      {!batches.length ? (
        <EmptyState icon={PackageOpen} title="No batches yet">Schedule clips first, then prepare a batch for the channel.</EmptyState>
      ) : (
        <div className="divide-y rounded-xl border">
          {batches.map((b) => (
            <div key={b.relative} className="flex flex-wrap items-center gap-3 p-4 text-sm">
              <div className="min-w-0 flex-1">
                <div className="font-medium">{b.channel} · {b.name.replace('_', ' ')}</div>
                <div className="truncate text-xs text-muted-foreground">{b.count} clips · {b.folder}</div>
              </div>
              <CopyButton text={b.folder} label="Copy folder path" />
              <Button size="sm" asChild><a href={b.url} target="_blank" rel="noreferrer"><ExternalLink /> Open checklist</a></Button>
            </div>
          ))}
        </div>
      )}
    </>
  )
}
