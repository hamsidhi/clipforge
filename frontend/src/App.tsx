import { Navigate, Route, Routes } from 'react-router-dom'
import Layout from '@/components/Layout'
import Dashboard from '@/pages/Dashboard'
import NewJob from '@/pages/NewJob'
import JobProgress from '@/pages/JobProgress'
import Gallery from '@/pages/Gallery'
import ClipEditor from '@/pages/ClipEditor'
import PublishKit from '@/pages/PublishKit'
import Tracker from '@/pages/Tracker'
import Settings from '@/pages/Settings'
import SystemCheck from '@/pages/SystemCheck'
import Channels from '@/pages/Channels'
import Schedule from '@/pages/Schedule'
import Batches from '@/pages/Batches'
import PublishDashboard from '@/pages/PublishDashboard'

export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Dashboard />} />
        <Route path="new" element={<NewJob />} />
        <Route path="jobs/:jobId" element={<JobProgress />} />
        <Route path="jobs/:jobId/clips" element={<Gallery />} />
        <Route path="clips/:clipId/edit" element={<ClipEditor />} />
        <Route path="clips/:clipId/publish" element={<PublishKit />} />
        <Route path="tracker" element={<Tracker />} />
        <Route path="settings" element={<Settings />} />
        <Route path="system" element={<SystemCheck />} />
        <Route path="channels" element={<Channels />} />
        <Route path="schedule" element={<Schedule />} />
        <Route path="batches" element={<Batches />} />
        <Route path="dashboard" element={<PublishDashboard />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  )
}
