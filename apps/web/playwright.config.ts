import { defineConfig, devices } from "@playwright/test";

const origin = new URL(process.env.FORGE_E2E_WEB_ORIGIN ?? "http://127.0.0.1:3000");
if (
  origin.protocol !== "http:" ||
  !["127.0.0.1", "localhost", "[::1]"].includes(origin.hostname) ||
  origin.username || origin.password || origin.pathname !== "/" || origin.search || origin.hash
) {
  throw new Error("Forge browser acceptance requires a loopback HTTP origin");
}

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/*.spec.ts",
  fullyParallel: false,
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  timeout: 240_000,
  expect: { timeout: 15_000 },
  reporter: process.env.CI ? [["list"], ["junit", { outputFile: "test-results/browser-acceptance.xml" }]] : "list",
  use: {
    baseURL: origin.origin,
    actionTimeout: 20_000,
    navigationTimeout: 30_000,
    trace: "off",
    video: "off",
    screenshot: "off",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
