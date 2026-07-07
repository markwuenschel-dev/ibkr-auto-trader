import { fileURLToPath } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./vitest.setup.ts"],
    include: ["**/*.test.ts", "**/*.test.tsx"],
    exclude: ["node_modules", ".next", "e2e"], // e2e is Playwright's, not Vitest's
  },
  resolve: {
    // mirror the tsconfig "@/*" -> project root alias
    alias: { "@": fileURLToPath(new URL(".", import.meta.url)) },
  },
});
