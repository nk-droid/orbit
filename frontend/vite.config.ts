import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'
import { VitePWA } from 'vite-plugin-pwa'

// The backend API (backend/src/api) is a separate process. Its routes are proxied so the
// frontend can call /api/* on its own origin. Point ORBIT_API_URL elsewhere if it moves.
const API_TARGET = process.env.ORBIT_API_URL ?? 'http://127.0.0.1:8000'

// https://vite.dev/config/
export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      registerType: 'autoUpdate',
      includeAssets: ['favicon.svg', 'apple-touch-icon.png'],
      manifest: {
        name: 'Orbit',
        short_name: 'Orbit',
        description: 'Voice-first personal assistant',
        theme_color: '#04070c',
        background_color: '#04070c',
        display: 'standalone',
        start_url: '/',
        icons: [
          { src: 'pwa-192.png', sizes: '192x192', type: 'image/png' },
          { src: 'pwa-512.png', sizes: '512x512', type: 'image/png' },
          { src: 'pwa-512.png', sizes: '512x512', type: 'image/png', purpose: 'maskable' },
        ],
      },
      workbox: {
        globPatterns: ['**/*.{js,css,html,svg,png,woff2}'],
        // API calls must always reach the backend, never the cached app shell.
        navigateFallbackDenylist: [/^\/api\//],
      },
    }),
  ],
  server: { proxy: { '/api': API_TARGET } },
  preview: { proxy: { '/api': API_TARGET } },
})
