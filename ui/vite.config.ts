import { defineConfig, loadEnv } from 'vite'
import path from 'path'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'

const BUILD_ID = String(Date.now())
const UI_ROOT = path.resolve(__dirname)

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, UI_ROOT, '')
  const viteApiBase = (env.VITE_API_BASE || 'http://127.0.0.1:8000').replace(/\/$/, '')

  return {
  plugins: [
    // The React and Tailwind plugins are both required for Make, even if
    // Tailwind is not being actively used – do not remove them
    react(),
    tailwindcss(),
  ],
  resolve: {
    alias: {
      // Alias @ to the src directory
      '@': path.resolve(__dirname, './src'),
    },
  },

  // File types to support raw imports. Never add .css, .tsx, or .ts files to this.
  assetsInclude: ['**/*.svg', '**/*.csv'],

  // Inject a build id so every build emits a distinct chunk hash — prevents
  // browsers from serving a stale bundle when the content-hash happens to
  // collide across builds with the same entry graph.
  envDir: UI_ROOT,
  define: {
    __MYTBOT_BUILD_ID__: JSON.stringify(BUILD_ID),
    // Baked-in default matches `python run.py` / uvicorn so Vite dev never hits :5173 as “API”.
    'import.meta.env.VITE_API_BASE': JSON.stringify(viteApiBase),
  },

  server: {
    port: 5173,
    strictPort: false,
  },

  build: {
    rollupOptions: {
      output: {
        // Force a unique hash window so any change to source produces a new
        // filename; dashed format keeps the old cache-busting contract.
        entryFileNames: `assets/[name]-[hash].js`,
        chunkFileNames: `assets/[name]-[hash].js`,
        assetFileNames: `assets/[name]-[hash][extname]`,
      },
    },
  },
  }
})
