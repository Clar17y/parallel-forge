import { cleanup, render } from "@testing-library/react";
import { screen } from "@testing-library/dom";
import { Navigation } from "./navigation";
import { afterEach, expect, test, vi } from "vitest";
vi.mock("next/navigation", () => ({ usePathname: () => "/runs" }));
afterEach(cleanup);
test("renders dashboard navigation with active route", () => {
  render(<Navigation />);
  expect(screen.getByRole("link", { name: "Runs" })).toHaveAttribute("aria-current", "page");
  for (const name of ['Home', 'Approvals', 'Projects', 'Epics', 'Policies', 'Agents & models', 'Tool permissions', 'Evaluations', 'Audit log', 'Usage', 'Subscription profiles']) {
    expect(screen.getByRole('link', { name })).toBeInTheDocument();
  }
  expect(screen.getByRole('link', { name: 'Home' })).toHaveAttribute('href', '/');
  expect(screen.getByRole('link', { name: 'Tool permissions' })).toHaveAttribute('href', '/tools');
  expect(screen.getByRole('link', { name: 'Epics' })).toHaveAttribute('href', '/epics');
});
test("badges the approvals link only when a pending count is supplied", () => {
  const { rerender } = render(<Navigation pendingApprovals="3" />);
  expect(screen.getByRole("link", { name: /Approvals 3/ })).toBeInTheDocument();
  rerender(<Navigation />);
  expect(screen.getByRole("link", { name: "Approvals" })).toBeInTheDocument();
});
