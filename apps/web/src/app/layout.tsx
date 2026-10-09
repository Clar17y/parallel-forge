import "./globals.css";
import { AppShell } from "@/components/layout/app-shell";
import { BootstrapGate } from "@/components/auth/bootstrap-gate";
import { ThemeProvider } from "@/components/theme/theme-provider";
import { themeBootstrapScript } from "@/lib/theme";
export const metadata = { title: "Forge", description: "Forge development workspace" };
export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en" suppressHydrationWarning><head><script dangerouslySetInnerHTML={{ __html: themeBootstrapScript }} /></head><body>
    <ThemeProvider><BootstrapGate><AppShell>{children}</AppShell></BootstrapGate></ThemeProvider>
  </body></html>;
}
