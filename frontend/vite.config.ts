import path from 'node:path'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// Dev: `npm run dev` proxies /api to the FastAPI server (clipforge ui --dev starts both).
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { '@': path.resolve(import.meta.dirname, './src') },
  },
  server: {
    port: 5173,
    proxy: {
      // xfwd: the API must see who is really asking (a phone on the LAN is not "this laptop")
      '/api': { target: 'http://127.0.0.1:8000', xfwd: true },
    },
  },
  build: {
    chunkSizeWarningLimit: 1500,
  },
})
