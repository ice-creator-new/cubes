import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The desktop shell (web/electron) loads the dev server from this fixed port.
export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  server: {
    host: "127.0.0.1",
    port: 1420,
    strictPort: true,
    watch: { ignored: ["**/electron/**"] },
  },
  build: {
    target: "es2022",
    sourcemap: false,
  },
});
