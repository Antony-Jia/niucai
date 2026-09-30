import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
export default defineConfig({
  plugins: [react()],
  server: {
    port: 1421,
    strictPort: true,
    fs: { allow: ["../.."] },
    proxy: {
      "/api": { target: "http://127.0.0.1:8080", ws: true },
      "/computer": { target: "http://127.0.0.1:8080", ws: true },
    },
  },
  preview: { port: 1421, strictPort: true },
  resolve: { dedupe: ["react", "react-dom", "@tanstack/react-query"] },
  build: { target: "es2022" },
});
