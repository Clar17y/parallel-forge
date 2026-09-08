import { render } from "@testing-library/react";
import { screen } from "@testing-library/dom";
import { BootstrapGate } from "./bootstrap-gate";
import { expect, test, vi } from "vitest";
test("exchanges fragment and removes it from history", async () => { window.location.hash="#bootstrap=one-time"; const exchange=vi.fn().mockResolvedValue({csrfToken:"csrf"}); const session=vi.fn().mockResolvedValue({csrfToken:"csrf"}); const replace=vi.spyOn(window.history,"replaceState"); render(<BootstrapGate exchange={exchange} session={session}><div>Dashboard</div></BootstrapGate>); expect(await screen.findByText("Dashboard")).toBeInTheDocument(); expect(exchange).toHaveBeenCalledWith("one-time", expect.any(AbortSignal)); expect(replace).toHaveBeenCalled(); });
