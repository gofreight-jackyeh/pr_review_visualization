/// <reference types="vitest/config" />
import { defineConfig } from "vite"
import react from "@vitejs/plugin-react"
import { fileURLToPath } from "node:url"

export default defineConfig({
  plugins: [react()],
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  server: { host: "127.0.0.1", port: 5173, proxy: { "/api": "http://127.0.0.1:8766" } },
  preview: { host: "127.0.0.1", port: 4173, proxy: { "/api": "http://127.0.0.1:8766" } },
  build: { outDir: "dist", chunkSizeWarningLimit: 6000 },
  test: { environment: "node", include: ["src/**/*.test.ts", "src/**/*.test.tsx"] },
})
