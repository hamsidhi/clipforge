import { Link, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ArrowLeft, BarChart3, Camera, Download, MonitorPlay, Pencil } from 'lucide-react'
import { api } from '@/api/client'
import { CopyButton, ErrorBox } from '@/components/common'
import PhoneFramePlayer from '@/components/PhoneFramePlayer'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'

function Field({ label, text, mono = false }: { label: string; text: string; mono?: boolean }) {
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm font-medium">{label}</span>
        <CopyButton text={text} />
      </div>
      <div className={`rounded-lg border bg-muted/30 p-3 text-sm whitespace-pre-wrap ${mono ? 'font-mono text-xs' : ''}`}>{text}</div>
    </div>
  )
}

export default function PublishKit() {
  const clipId = useParams().clipId!
  const jobId = Number(clipId.split('-')[0])
  const { data: clip, error } = useQuery({ queryKey: ['clip', clipId], queryFn: () => api.clip(clipId) })
  if (error) return <ErrorBox error={error} />
  if (!clip) return <Skeleton className="h-96 rounded-2xl" />
  const m = clip.metadata
  return (
    <>
      <div className="mb-5 flex flex-wrap items-center gap-2">
        <Button variant="ghost" size="sm" asChild>
          <Link to={`/jobs/${jobId}/clips`}><ArrowLeft /> Gallery</Link>
        </Button>
        <h1 className="text-lg font-semibold">Publish kit · #{clip.rank} {clip.title}</h1>
      </div>
      <div className="grid gap-6 lg:grid-cols-[300px_1fr]">
        <div className="space-y-3">
          <PhoneFramePlayer src={clip.video_url} poster={clip.thumbnail_url} className="max-w-[300px]" placeholder="Not rendered yet" />
          <div className="mx-auto grid max-w-[300px] gap-2">
            {clip.video_url && (
              <Button asChild>
                <a href={`/api/clips/${clipId}/video?download=true`}><Download /> Download mp4</a>
              </Button>
            )}
            <Button variant="outline" asChild>
              <Link to={`/clips/${clipId}/edit`}><Pencil /> Edit clip</Link>
            </Button>
            <Button variant="outline" asChild>
              <Link to={`/tracker?job=${jobId}&rank=${clip.rank}&title=${encodeURIComponent(clip.title)}`}><BarChart3 /> Log as posted</Link>
            </Button>
          </div>
        </div>
        {!m ? (
          <Card>
            <CardContent className="py-10 text-center text-sm text-muted-foreground">Metadata is generated after the clip is rendered.</CardContent>
          </Card>
        ) : (
          <div className="space-y-6">
            <Card>
              <CardHeader>
                <CardTitle className="flex items-center gap-2"><MonitorPlay className="size-5 text-red-500" /> YouTube Shorts</CardTitle>
              </CardHeader>
              <CardContent className="space-y-5">
                <Field label="Title" text={m.yt_title} />
                <Field label="Description" text={m.yt_description} />
              </CardContent>
            </Card>
            <Card>
              <CardHeader>
                <CardTitle className="flex items-center gap-2"><Camera className="size-5 text-pink-500" /> Instagram Reels</CardTitle>
              </CardHeader>
              <CardContent className="space-y-5">
                <Field label="Caption" text={m.ig_caption} />
              </CardContent>
            </Card>
            <Card>
              <CardContent className="space-y-5">
                <Field label={`Hashtags (${m.hashtags.length})`} text={m.hashtags.join(' ')} mono />
                <Field label="Source credit" text={m.credit} />
              </CardContent>
            </Card>
          </div>
        )}
      </div>
    </>
  )
}
