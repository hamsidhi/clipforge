import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import {
  BarChart3, CalendarClock, Clapperboard, FolderUp, HeartPulse, LayoutGrid, LineChart, MoreHorizontal, Plus, Settings as SettingsIcon, Tv,
} from 'lucide-react'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { cn } from '@/lib/utils'

const GROUPS = [
  {
    label: 'Make',
    items: [
      { to: '/', label: 'Jobs', icon: LayoutGrid, end: true },
      { to: '/new', label: 'New job', icon: Plus },
    ],
  },
  {
    label: 'Publish',
    items: [
      { to: '/schedule', label: 'Schedule', icon: CalendarClock },
      { to: '/batches', label: 'Batches', icon: FolderUp },
      { to: '/dashboard', label: 'Dashboard', icon: LineChart },
      { to: '/tracker', label: 'Tracker', icon: BarChart3 },
      { to: '/channels', label: 'Accounts', icon: Tv },
    ],
  },
  {
    label: 'App',
    items: [
      { to: '/settings', label: 'Settings', icon: SettingsIcon },
      { to: '/system', label: 'System', icon: HeartPulse },
    ],
  },
]
const MOBILE_MAIN = [
  { to: '/', label: 'Jobs', icon: LayoutGrid, end: true },
  { to: '/new', label: 'New', icon: Plus },
  { to: '/schedule', label: 'Schedule', icon: CalendarClock },
  { to: '/dashboard', label: 'Stats', icon: LineChart },
]
const MOBILE_MORE = [
  { to: '/batches', label: 'Batches', icon: FolderUp },
  { to: '/tracker', label: 'Tracker', icon: BarChart3 },
  { to: '/channels', label: 'Accounts', icon: Tv },
  { to: '/settings', label: 'Settings', icon: SettingsIcon },
  { to: '/system', label: 'System', icon: HeartPulse },
]

function Brand() {
  return (
    <div className="flex items-center gap-2.5">
      <div className="grid size-8 place-items-center rounded-lg bg-primary text-primary-foreground">
        <Clapperboard className="size-4.5" strokeWidth={2.5} />
      </div>
      <div className="leading-tight">
        <div className="font-semibold tracking-tight">ClipForge</div>
        <div className="text-[11px] text-muted-foreground">podcast → shorts</div>
      </div>
    </div>
  )
}

export default function Layout() {
  const { pathname } = useLocation()
  const nav = useNavigate()
  const wide = pathname.includes('/edit') || pathname.startsWith('/schedule')
  const moreActive = MOBILE_MORE.some((m) => pathname.startsWith(m.to))
  return (
    <div className="min-h-dvh bg-background">
      <aside className="fixed inset-y-0 left-0 z-30 hidden w-56 flex-col border-r border-sidebar-border bg-sidebar px-3 py-5 md:flex">
        <div className="px-2"><Brand /></div>
        <nav className="mt-6 flex flex-col gap-5 overflow-y-auto">
          {GROUPS.map((g) => (
            <div key={g.label} className="flex flex-col gap-1">
              <div className="px-3 text-[10px] font-semibold tracking-wider text-muted-foreground/70 uppercase">{g.label}</div>
              {g.items.map(({ to, label, icon: Icon, end }) => (
                <NavLink
                  key={to}
                  to={to}
                  end={end}
                  className={({ isActive }) =>
                    cn('flex items-center gap-3 rounded-lg px-3 py-2 text-sm text-muted-foreground transition-colors hover:bg-sidebar-accent hover:text-foreground',
                      isActive && 'bg-sidebar-accent font-medium text-foreground')
                  }
                >
                  <Icon className="size-4" />
                  {label}
                </NavLink>
              ))}
            </div>
          ))}
        </nav>
        <div className="mt-auto px-3 pt-4 text-[11px] leading-relaxed text-muted-foreground">100% free &amp; local.<br />No watermark, ever.</div>
      </aside>

      <header className="sticky top-0 z-30 flex items-center justify-between border-b bg-background/85 px-4 py-3 backdrop-blur md:hidden">
        <Brand />
      </header>

      <main className="pb-24 md:pb-10 md:pl-56">
        <div className={cn('mx-auto px-4 py-6 md:px-8 md:py-8', wide ? 'max-w-[1500px]' : 'max-w-6xl')}>
          <Outlet />
        </div>
      </main>

      <nav className="fixed inset-x-0 bottom-0 z-30 grid grid-cols-5 border-t bg-background/95 pb-[env(safe-area-inset-bottom)] backdrop-blur md:hidden">
        {MOBILE_MAIN.map(({ to, label, icon: Icon, end }) => (
          <NavLink key={to} to={to} end={end}
            className={({ isActive }) => cn('flex flex-col items-center gap-1 py-2.5 text-[10px] text-muted-foreground', isActive && 'text-primary')}>
            <Icon className="size-5" />
            {label}
          </NavLink>
        ))}
        <DropdownMenu>
          <DropdownMenuTrigger className={cn('flex flex-col items-center gap-1 py-2.5 text-[10px] text-muted-foreground', moreActive && 'text-primary')}>
            <MoreHorizontal className="size-5" />
            More
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" side="top">
            {MOBILE_MORE.map(({ to, label, icon: Icon }) => (
              <DropdownMenuItem key={to} onClick={() => nav(to)}><Icon /> {label}</DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
      </nav>
    </div>
  )
}
