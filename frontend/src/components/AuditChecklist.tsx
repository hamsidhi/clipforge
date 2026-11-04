import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ClipboardCheck, ExternalLink } from 'lucide-react'
import { toast } from 'sonner'
import { pub, usePublishing } from '@/api/publish'
import type { AuditStatus } from '@/api/publishTypes'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'

const REPO = 'https://github.com/hamsidhi/clipforge'
const SITE = 'https://hamsidhi.github.io/clipforge'

/** Settings → Publishing → "Apply for API audit": the steps + where you are. Approved → api_audited = true. */
export default function AuditChecklist() {
  const qc = useQueryClient()
  const { data: st } = usePublishing()
  const [date, setDate] = useState('')
  const save = useMutation({
    mutationFn: ({ status, d }: { status: AuditStatus; d?: string }) => pub.setAudit(status, d),
    onSuccess: (r) => {
      toast.success(r.audit_status === 'approved' ? 'Marked approved — API mode is now the recommended default' : 'Saved')
      qc.invalidateQueries({ queryKey: ['publishing'] })
      qc.invalidateQueries({ queryKey: ['health'] })
    },
    onError: (e) => toast.error(e.message),
  })
  if (!st) return null
  return (
    <div id="audit" className="rounded-xl border p-4">
      <div className="mb-2 flex items-center gap-2 font-medium"><ClipboardCheck className="size-4" /> Apply for the YouTube API audit (optional, free)</div>
      <p className="mb-3 text-sm text-muted-foreground">
        Since 2 Nov 2026 Google no longer locks uploads from unaudited projects to private, so API mode publishes without it.
        The audit raises your quota above 100 uploads/day and is the official "compliant app" stamp. Approval isn't guaranteed
        for personal tools; assisted mode always works meanwhile.
      </p>
      <ol className="mb-4 list-decimal space-y-1 pl-5 text-sm text-muted-foreground">
        <li>Check the public pages are live: <a className="text-primary underline" href={`${SITE}/privacy.html`} target="_blank" rel="noreferrer">Privacy Policy</a> and{' '}
          <a className="text-primary underline" href={`${SITE}/terms.html`} target="_blank" rel="noreferrer">Terms</a>.</li>
        <li>Open <a className="text-primary underline" href={`${REPO}/blob/main/AUDIT_KIT.md`} target="_blank" rel="noreferrer">AUDIT_KIT.md</a>: copy the answers, attach the screenshots + screencast from <code>docs/audit/</code>.</li>
        <li>Fill in your Google Cloud <b>project number</b> (Cloud console → Dashboard).</li>
        <li>Send the <a className="text-primary underline" href={st.audit_form} target="_blank" rel="noreferrer">YouTube API Services audit form</a>, then mark it “Applied” below.</li>
      </ol>
      <div className="flex flex-wrap items-end gap-3">
        <div className="space-y-1.5">
          <Label>Status</Label>
          <Select value={st.audit_status} onValueChange={(v) => save.mutate({ status: v as AuditStatus, d: v === 'applied' ? date || new Date().toISOString().slice(0, 10) : undefined })}>
            <SelectTrigger className="w-56"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value="not_applied">Not applied</SelectItem>
              <SelectItem value="applied">Applied</SelectItem>
              <SelectItem value="approved">Approved</SelectItem>
              <SelectItem value="rejected">Rejected</SelectItem>
            </SelectContent>
          </Select>
        </div>
        <div className="space-y-1.5">
          <Label>Applied on</Label>
          <Input type="date" className="w-44" value={date || st.audit_applied_on} onChange={(e) => setDate(e.target.value)}
            onBlur={() => date && save.mutate({ status: st.audit_status === 'not_applied' ? 'applied' : st.audit_status, d: date })} />
        </div>
        <Button variant="ghost" asChild><a href={st.audit_form} target="_blank" rel="noreferrer"><ExternalLink /> Audit form</a></Button>
      </div>
    </div>
  )
}
