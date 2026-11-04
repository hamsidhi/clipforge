import { Navigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Film } from 'lucide-react'
import { api } from '@/api/client'
import { EmptyState } from '@/components/common'
import { Skeleton } from '@/components/ui/skeleton'

/** Phone bottom bar "Gallery": the newest finished episode's clips. */
export default function LatestGallery() {
  const { data: jobs, isLoading } = useQuery({ queryKey: ['jobs'], queryFn: api.jobs })
  if (isLoading) return <Skeleton className="h-96 rounded-xl" />
  const latest = (jobs ?? []).find((j) => j.status === 'done' && j.n_clips > 0)
  if (latest) return <Navigate to={`/jobs/${latest.id}/clips`} replace />
  return <EmptyState icon={Film} title="No clips yet">Start a job: share a YouTube link to ClipForge, or tap Jobs → New.</EmptyState>
}
