"use client";

import { usePathname } from "next/navigation";
import { AccountProvider, useAccount } from "@/components/account";
import { AppMemoryProvider } from "@/components/app-memory";
import { AppShell } from "@/components/app-shell";
import { WorkspaceProvider } from "@/components/workspace";

function MemoryBoundary({ children }: { children: React.ReactNode }) {
  const { account } = useAccount();
  return <AppMemoryProvider key={account?.account_id || "anonymous"}>{children}</AppMemoryProvider>;
}

export function ShellBoundary({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();

  if (pathname === "/lock") {
    return <>{children}</>;
  }

  return (
    <AccountProvider>
      <WorkspaceProvider>
        <MemoryBoundary>
          <AppShell>{children}</AppShell>
        </MemoryBoundary>
      </WorkspaceProvider>
    </AccountProvider>
  );
}
