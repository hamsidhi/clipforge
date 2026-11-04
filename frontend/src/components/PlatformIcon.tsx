import { Camera, MonitorPlay } from 'lucide-react'
import type { Platform } from '@/api/publishTypes'
import { cn } from '@/lib/utils'

/** Small platform glyph: YouTube = red play screen, Instagram = pink camera (no brand logos). */
export default function PlatformIcon({ platform, className }: { platform?: Platform | string; className?: string }) {
  return platform === 'instagram'
    ? <Camera aria-label="Instagram" className={cn('size-3.5 shrink-0 text-pink-400', className)} />
    : <MonitorPlay aria-label="YouTube" className={cn('size-3.5 shrink-0 text-red-500', className)} />
}

export const PLATFORM_LABEL: Record<string, string> = { youtube: 'YouTube', instagram: 'Instagram' }
