import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// `npm run build` writes fixed file names into ../static/app so Flask can serve them.
// `npm run dev` serves the UI on :5173 and proxies /api to Flask on :5000.
export default defineConfig({
  plugins: [react()],
  base: '/static/app/',
  build: {
    outDir: '../static/app',
    emptyOutDir: true,
    rollupOptions: { output: { entryFileNames: 'app.js', chunkFileNames: 'chunk-[name].js', assetFileNames: 'app.[ext]' } },
  },
  server: { proxy: { '/api': 'http://127.0.0.1:5000' } },
});
