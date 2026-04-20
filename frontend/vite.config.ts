import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    proxy: {
      "/api": "http://localhost:8000",
      "/stream": "http://localhost:8000",
      "/chat": "http://localhost:8000",
      "/thumbnails": "http://localhost:8000",
      "/admin/video_feed": "http://localhost:8000",
      "/admin/detections": "http://localhost:8000",
    },
  },
  build: {
    outDir: "dist",
  },
});
