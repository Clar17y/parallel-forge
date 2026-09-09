import { render } from "@testing-library/react";
import { screen } from "@testing-library/dom";
import { Navigation } from "./navigation";
import { expect, test, vi } from "vitest";
vi.mock("next/navigation", () => ({ usePathname: () => "/runs" }));
test("renders dashboard navigation with active route", () => {
  render(<Navigation />);
  expect(screen.getByRole("link", { name: "Runs" })).toHaveAttribute("aria-current", "page");
  for (const name of ['Approvals', 'Projects', 'Policies', 'Agents & models', 'Tool permissions', 'Evaluations', 'Audit log', 'Usage']) {
    expect(screen.getByRole('link', { name })).toBeInTheDocument();
  }
  expect(screen.getByRole('link', { name: 'Tool permissions' })).toHaveAttribute('href', '/tools');
});
