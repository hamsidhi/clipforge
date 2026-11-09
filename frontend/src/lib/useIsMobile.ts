import { useEffect, useState } from 'react'

/** True below the `lg` breakpoint (phones, small tablets): the clip editor switches to its one-thing-at-a-time layout. */
export function useIsMobile(query = '(max-width: 1023px)'): boolean {
  const [match, setMatch] = useState(() => typeof window !== 'undefined' && window.matchMedia(query).matches)
  useEffect(() => {
    const mq = window.matchMedia(query)
    const on = () => setMatch(mq.matches)
    on()
    mq.addEventListener('change', on)
    return () => mq.removeEventListener('change', on)
  }, [query])
  return match
}
