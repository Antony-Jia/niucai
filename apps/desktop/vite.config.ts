import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  server: {
    port: 1420,
    strictPort: true,
    watch: { ignored: ["**/src-tauri/**"] },
  },
  envPrefix: ["VITE_"],
  build: { target: "es2022" },
  test: {
    include: ["tests/**/*.test.tsx"],
    environment: "jsdom",
    setupFiles: ["./tests/setup.ts"],
    css: false,
  },
});
