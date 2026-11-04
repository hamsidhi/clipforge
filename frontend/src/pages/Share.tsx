import { Navigate, useSearchParams } from 'react-router-dom'

/** Android share target: sharing a YouTube link from the YouTube app opens New job with it filled in.
 *  Apps put the link in `url`, `text` (often "Title https://youtu.be/…") or `title`. */
export default function Share() {
  const [p] = useSearchParams()
  const all = [p.get('url'), p.get('text'), p.get('title')].filter(Boolean).join(' ')
  const link = all.match(/https?:\/\/\S+/)?.[0] ?? ''
  return <Navigate to={link ? `/new?url=${encodeURIComponent(link)}` : '/new'} replace />
}
