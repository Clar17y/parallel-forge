import "./globals.css";
import { AppShell } from "@/components/layout/app-shell";
import { BootstrapGate } from "@/components/auth/bootstrap-gate";
export const metadata = { title: "Forge", description: "Forge development workspace" };
export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>
    <BootstrapGate><AppShell>{children}</AppShell></BootstrapGate>
  </body></html>;
}
