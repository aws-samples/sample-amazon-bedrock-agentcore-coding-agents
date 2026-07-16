import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import path from 'node:path';

// The built app lands in ../dist (console/dist), which server.py serves at
// the same origin as the /api/dev|orchestrator|metrics mounts. base:'/' because the page is
// hosted at the root path by server.py.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: '/',
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
      // The foxl product code/ui were copied into this repo (no cross-repo dep).
      '@foxl/code': path.resolve(__dirname, './src/vendor/code'),
      '@foxl/ui': path.resolve(__dirname, './src/vendor/ui'),
      '@foxl/types': path.resolve(__dirname, './src/vendor/types'),
    },
  },
  build: {
    outDir: path.resolve(__dirname, '../dist'),
    emptyOutDir: true,
    target: 'es2022',
    sourcemap: false,
    // Split heavy, independently-cacheable vendor groups out of the entry so
    // first paint isn't one giant chunk. The markdown + syntax-highlighter
    // stack (huge) and the xterm terminal emulator only matter on the chat and
    // workspace screens respectively, so they get their own chunks that the
    // browser fetches on demand and caches across navigations.
    chunkSizeWarningLimit: 900,
    rollupOptions: {
      output: {
        manualChunks: {
          'react-vendor': ['react', 'react-dom', 'react-router-dom'],
          // NOTE: deliberately NO 'highlight.js' entry. Listing the package
          // here forces ALL ~190 grammars into the chunk and defeats the
          // per-language tree-shaking in FleetsPage (HLJS_LANGUAGES). Letting
          // it fold into the markdown chunk keeps only the langs we register.
          markdown: ['react-markdown', 'remark-gfm', 'rehype-highlight'],
          xterm: ['xterm', '@xterm/addon-fit'],
        },
      },
    },
  },
  server: {
    port: 5174,
    strictPort: false,
    // HMR works on EITHER entry port. We do NOT hardcode the HMR clientPort:
    // Vite's client then infers the websocket host/port from the page origin, so
    //   • open :5174 directly  → HMR dials :5174 (Vite serves it)
    //   • open :8080 (backend, CONSOLE_DEV=1) → the page origin is :8080, the
    //     backend reverse-proxies the HMR websocket through to Vite.
    // Either way hot updates reach the browser. Both ports also proxy /api to the
    // backend below, so the API is reachable from :5174 too.
    proxy: {
      '/api': {
        // Backend port is configurable (CONSOLE_PORT). VS Code Server often
        // occupies :8080, so allow an override via VITE_API_TARGET and default
        // to :8088 to stay clear of it.
        target: process.env.VITE_API_TARGET || 'http://localhost:8088',
        changeOrigin: true,
      },
    },
  },
});
