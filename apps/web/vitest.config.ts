import { configDefaults, defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";
export default defineConfig({
  resolve: { alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) } },
  plugins: [react()],
  test: {
    // Form tests type long values with user-event. On a busy machine that outlasts
    // the 5 s default, and a timed-out test keeps typing into the next one.
    testTimeout: 20_000,
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
    exclude: [...configDefaults.exclude, "e2e/**"],
  },
});
