import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Build output goes to client/dist, which FastAPI serves as the SPA in production.
// In dev, /api and /mcp are proxied to the local FastAPI server on :8000.
export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist" },
  server: {
    proxy: {
      "/api": "http://localhost:8000",
      "/mcp": "http://localhost:8000",
    },
  },
});
