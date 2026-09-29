import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174,
    proxy: {
      // ws:true is required for the WebSocket streams under /api/stream. Without
      // it Vite proxies the HTTP request but not the Upgrade handshake, and the
      // socket fails only behind the dev server -- which is exactly the kind of
      // difference between dev and prod that costs an afternoon.
      "/api": { target: "http://localhost:8000", ws: true, changeOrigin: true },
      "/metrics": { target: "http://localhost:8000", changeOrigin: true },
    },
  },
});
