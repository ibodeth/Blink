import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The production build is emitted to ./dist and served directly by FastAPI
// (see src/server.py). During development the Vite dev server runs on 5173 and
// proxies nothing — the React app opens its own WebSocket to the backend port.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: false,
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
