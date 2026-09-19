import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  server: {
    // Reachable from a phone or tablet on the same network as the Pi.
    host: true,
    allowedHosts: ["astropi.local"],
    proxy: {
      // Dev-only: lets the app call same-origin paths, so no base URL is
      // needed in the client and cookies/CORS behave as they will in
      // production, where the backend serves the built bundle itself.
      "/api": { target: "http://localhost:8000", changeOrigin: true },
      "/ws": { target: "ws://localhost:8000", ws: true },
    },
  },
});
