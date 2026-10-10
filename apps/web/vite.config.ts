import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
const apiTarget = `http://127.0.0.1:${process.env.NIUCAI_TEST_API_PORT || "8080"}`;
export default defineConfig({
  plugins: [react()],
  server: {
    port: 1421,
    strictPort: true,
    fs: { allow: ["../.."] },
    proxy: {
      "/api": { target: apiTarget, ws: true },
      "/computer": { target: apiTarget, ws: true },
    },
  },
  preview: { port: 1421, strictPort: true },
  resolve: { dedupe: ["react", "react-dom", "@tanstack/react-query"] },
  build: { target: "es2022" },
});
