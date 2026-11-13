import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

// Dev proxy: the browser calls /api/*, Vite forwards to FastAPI with the prefix stripped.
// In Compose the target is the api service (VITE_PROXY_TARGET=http://api:8000).
const target = process.env.VITE_PROXY_TARGET ?? 'http://localhost:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target, changeOrigin: true, rewrite: (path) => path.replace(/^\/api/, '') },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    css: false,
  },
})
