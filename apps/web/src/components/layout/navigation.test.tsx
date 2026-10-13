import { cleanup, render, within } from "@testing-library/react";
import { screen } from "@testing-library/dom";
import { Navigation } from "./navigation";
import { afterEach, expect, test, vi } from "vitest";
vi.mock("next/navigation", () => ({ usePathname: () => "/runs" }));
afterEach(cleanup);
test("renders dashboard navigation with active route", () => {
  render(<Navigation />);
  expect(screen.getByRole("link", { name: "Runs" })).toHaveAttribute("aria-current", "page");
  for (const name of ['Approvals', 'Projects', 'Epics', 'Settings', 'Policies', 'Agents & models', 'Tool permissions', 'Evaluations', 'Audit log', 'Usage', 'Subscription profiles']) {
    expect(screen.getByRole('link', { name })).toBeInTheDocument();
  }
  expect(screen.getByRole('link', { name: 'Settings' })).toHaveAttribute('href', '/settings');
  expect(screen.getByRole('link', { name: 'Tool permissions' })).toHaveAttribute('href', '/tools');
  expect(screen.getByRole('link', { name: 'Epics' })).toHaveAttribute('href', '/epics');
});
test("read-only evaluations sit under Inspect, not the action-oriented Configure group", () => {
  render(<Navigation />);
  const inspect = within(screen.getByRole('region', { name: 'Inspect' }));
  expect(inspect.getByRole('link', { name: 'Evaluations' })).toHaveAttribute('href', '/evaluations');
  const configure = within(screen.getByRole('region', { name: 'Configure' }));
  expect(configure.queryByRole('link', { name: 'Evaluations' })).not.toBeInTheDocument();
  expect(configure.getByRole('link', { name: 'Settings' })).toBeInTheDocument();
});
