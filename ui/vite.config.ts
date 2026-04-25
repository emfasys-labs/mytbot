import { defineConfig } from 'vite'
import path from 'path'
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'

const BUILD_ID = String(Date.now())

export default defineConfig({
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
  define: {
    __MYTBOT_BUILD_ID__: JSON.stringify(BUILD_ID),
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
})
