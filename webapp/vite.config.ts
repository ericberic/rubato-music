import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

export default defineConfig({
  plugins: [svelte()],
  server: {
    // The built app is served by the Python server at /app/ on the same origin,
    // so the generated client uses relative /api URLs. In dev, Vite serves the
    // UI and proxies those to uvicorn, keeping one code path for both instead
    // of a dev-only base URL that can drift from production.
    proxy: {
      // Exactly one API server, always on 8000. A second one on another port
      // is a footgun: the UI looks identical while serving different code, and
      // you cannot tell which by looking. Start it with preview_start "api";
      // if the port is busy, kill that process rather than moving aside.
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true, ws: true },
    },
  },
  build: {
    outDir: '../src/aimusic/server/static',
    emptyOutDir: true,
    sourcemap: true,
  },
  base: '/app/',
});
