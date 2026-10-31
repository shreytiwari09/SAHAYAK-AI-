import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the console talks to the Sahayak API through /api, so no CORS and the same
// path shape as production. In production the API serves the built console at /console.
export default defineConfig({
  base: "./",
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: process.env.SAHAYAK_API || "http://localhost:8000",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
  build: { outDir: "dist", sourcemap: false },
});
