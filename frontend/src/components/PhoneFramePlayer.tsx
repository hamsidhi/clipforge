import { forwardRef } from 'react'
import { Heart, MessageCircle, Send, Music2 } from 'lucide-react'
import { cn } from '@/lib/utils'

/** 9:16 phone-shaped player with an optional Reels/Shorts UI overlay to check caption safe zones. */
const PhoneFramePlayer = forwardRef<HTMLVideoElement, {
  src: string | null
  poster?: string | null
  safeZone?: boolean
  className?: string
  placeholder?: React.ReactNode
}>(function PhoneFramePlayer({ src, poster, safeZone, className, placeholder }, ref) {
  return (
    <div className={cn('phone-frame relative mx-auto w-full overflow-hidden bg-black', className)}>
      {src ? (
        <video ref={ref} key={src} src={src} poster={poster ?? undefined} controls playsInline className="absolute inset-0 size-full object-contain" />
      ) : (
        <div className="absolute inset-0 grid place-items-center p-6 text-center text-sm text-muted-foreground">{placeholder}</div>
      )}
      {safeZone && (
        <div className="pointer-events-none absolute inset-0">
          {/* top bar */}
          <div className="absolute inset-x-0 top-0 h-[9%] bg-gradient-to-b from-black/60 to-transparent">
            <div className="px-4 pt-3 text-[11px] font-semibold text-white/90">Reels</div>
          </div>
          {/* right action column */}
          <div className="absolute right-0 bottom-[14%] flex w-[13%] flex-col items-center gap-4 text-white/90">
            <Heart className="size-5" /> <MessageCircle className="size-5" /> <Send className="size-5" />
          </div>
          {/* bottom UI: username, caption, audio */}
          <div className="absolute inset-x-0 bottom-0 h-[20%] bg-gradient-to-t from-black/70 to-transparent">
            <div className="absolute bottom-[22%] left-3 space-y-1 text-[10px] text-white/90">
              <div className="font-semibold">@yourchannel</div>
              <div className="opacity-80">Caption text goes here…</div>
              <div className="flex items-center gap-1 opacity-80"><Music2 className="size-3" /> Original audio</div>
            </div>
          </div>
          <div className="absolute inset-x-0 bottom-0 h-[20%] border-t border-dashed border-red-400/70" />
          <div className="absolute top-0 right-0 bottom-0 w-[13%] border-l border-dashed border-red-400/50" />
        </div>
      )}
    </div>
  )
})

export default PhoneFramePlayer
