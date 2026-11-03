import { useQuery } from '@tanstack/react-query'
import { Check } from 'lucide-react'
import { api } from '@/api/client'
import type { Preset } from '@/api/types'
import { cn } from '@/lib/utils'

export const FONT_FAMILIES: Record<string, string> = {
  'Montserrat-ExtraBold': "'CF Montserrat', sans-serif",
  'Poppins-Bold': "'CF Poppins', sans-serif",
  'NotoSansDevanagari-Bold': "'CF Devanagari', sans-serif",
}

const SAMPLE_ROMAN = ['ye', 'galti', '100', 'crore']
const SAMPLE_DEVA = ['ये', 'गलती', '100', 'करोड़']

/** Mini live preview of a caption style (same fonts as the renderer). */
export function CaptionPreview({ preset, highlight, keyword, active = 1, className, size = 15 }: {
  preset: Pick<Preset, 'font' | 'highlight_color' | 'keyword_color' | 'uppercase' | 'text_color'>
  highlight?: string | null
  keyword?: string | null
  active?: number
  className?: string
  size?: number
}) {
  const deva = preset.font === 'NotoSansDevanagari-Bold'
  const words = deva ? SAMPLE_DEVA : SAMPLE_ROMAN
  return (
    <div
      className={cn('caption-text flex flex-wrap justify-center gap-x-[0.3em] leading-tight', className)}
      style={{ fontFamily: FONT_FAMILIES[preset.font] ?? 'sans-serif', fontSize: size, fontWeight: 800 }}
    >
      {words.map((w, i) => (
        <span
          key={i}
          style={{
            color: i === active ? highlight ?? preset.highlight_color : i === 2 ? keyword ?? preset.keyword_color : preset.text_color ?? '#fff',
            display: 'inline-block',
            transform: i === active ? 'scale(1.12)' : undefined,
          }}
        >
          {preset.uppercase && !deva ? w.toUpperCase() : w}
        </span>
      ))}
    </div>
  )
}

export default function CaptionStylePicker({ value, onChange, compact = false }: {
  value: string
  onChange: (key: string) => void
  compact?: boolean
}) {
  const { data: presets = [] } = useQuery({ queryKey: ['presets'], queryFn: api.presets, staleTime: 60_000 })
  return (
    <div className={cn('grid gap-3', compact ? 'grid-cols-2' : 'grid-cols-2 sm:grid-cols-4')}>
      {presets.map((p) => {
        const selected = p.key === value
        return (
          <button
            type="button"
            key={p.key}
            onClick={() => onChange(p.key)}
            className={cn(
              'relative overflow-hidden rounded-xl border text-left transition-all hover:border-primary/50',
              selected ? 'border-primary ring-2 ring-primary/30' : 'border-border',
            )}
          >
            <div className="grid h-20 place-items-center bg-[radial-gradient(ellipse_at_top,oklch(0.35_0.03_250),oklch(0.16_0.01_285))] px-2">
              <CaptionPreview preset={p} />
            </div>
            <div className="flex items-center justify-between px-3 py-2 text-xs font-medium">
              {p.label}
              {selected && <Check className="size-3.5 text-primary" />}
            </div>
          </button>
        )
      })}
    </div>
  )
}
