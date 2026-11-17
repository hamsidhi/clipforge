import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Check, Folder, FolderPlus } from 'lucide-react'
import { api } from '@/api/client'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { cn } from '@/lib/utils'

/** Pick an existing folder, create a new one, or "No folder" (value ""). */
export default function FolderPicker({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const { data: folders = [] } = useQuery({ queryKey: ['job-folders'], queryFn: api.jobFolders })
  const [creating, setCreating] = useState(false)
  const [name, setName] = useState('')
  const names = folders.map((f) => f.name)
  const extra = value && !names.includes(value) ? [value] : []  // a new folder typed here, not saved yet
  const chip = (label: string, v: string, icon = true) => (
    <button key={label} type="button" onClick={() => onChange(v)}
      className={cn('inline-flex max-w-full items-center gap-1.5 rounded-full border px-3 py-1 text-xs transition-colors',
        value === v ? 'border-primary bg-primary/15 text-foreground' : 'border-border text-muted-foreground hover:border-primary/50 hover:text-foreground')}>
      {value === v ? <Check className="size-3 shrink-0" /> : icon ? <Folder className="size-3 shrink-0" /> : null}
      <span className="truncate">{label}</span>
    </button>
  )
  const add = () => {
    const n = name.trim().replace(/\s+/g, ' ')
    if (!n) return
    onChange(names.find((x) => x.toLowerCase() === n.toLowerCase()) ?? n)
    setName('')
    setCreating(false)
  }
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-1.5">
        {chip('No folder', '', false)}
        {[...names, ...extra].map((n) => chip(n, n))}
        {!creating && (
          <button type="button" onClick={() => setCreating(true)}
            className="inline-flex items-center gap-1.5 rounded-full border border-dashed px-3 py-1 text-xs text-muted-foreground hover:border-primary/50 hover:text-foreground">
            <FolderPlus className="size-3" /> New folder
          </button>
        )}
      </div>
      {creating && (
        <div className="flex gap-2">
          <Input autoFocus value={name} placeholder="e.g. Raj Shamani, Gaming tips" maxLength={60}
            onChange={(e) => setName(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); add() } }} />
          <Button type="button" size="sm" className="h-9" onClick={add} disabled={!name.trim()}>Add</Button>
          <Button type="button" size="sm" variant="ghost" className="h-9" onClick={() => setCreating(false)}>Cancel</Button>
        </div>
      )}
    </div>
  )
}
