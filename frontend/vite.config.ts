import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev proxies default to the local backend. VITE_API_TARGET overrides it so the e2e suite can point
// at its OWN backend instead of whatever is on :8000 — on this project that's usually the live one.
const API_TARGET = process.env.VITE_API_TARGET ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: 5173,
    proxy: {
      "/api": {
        target: API_TARGET,
        changeOrigin: true,
      },
      "/covers": {
        target: API_TARGET,
        changeOrigin: true,
      },
      "/media": {
        target: API_TARGET,
        changeOrigin: true,
      },
    },
  },
});
