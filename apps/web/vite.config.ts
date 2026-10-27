import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const target = process.env.VITE_API_BASE ?? "http://127.0.0.1:8000";

export default defineConfig({
  base: "./",
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/v1": { target, changeOrigin: false },
      "/healthz": { target, changeOrigin: false },
    },
  },
  build: { outDir: "dist", sourcemap: false },
});
