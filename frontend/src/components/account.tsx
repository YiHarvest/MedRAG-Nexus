"use client";

import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";
import {
  getCurrentAccount,
  loginWebUiAccount,
  logoutWebUiAccount,
  registerAccount,
  WebUiApiError,
  type WebUiPrincipal,
} from "@/lib/webui-api";

interface AccountContextValue {
  principal: WebUiPrincipal | null;
  account: WebUiPrincipal["account"] | null;
  permissions: ReadonlySet<string>;
  authenticated: boolean;
  loading: boolean;
  can: (permission: string) => boolean;
  canAny: (permissions: readonly string[]) => boolean;
  login: (payload: { login_name: string; password: string }) => Promise<WebUiPrincipal>;
  register: (payload: {
    login_name: string;
    display_name: string;
    password: string;
  }) => Promise<WebUiPrincipal>;
  logout: () => Promise<void>;
  refresh: () => Promise<WebUiPrincipal | null>;
}

const AccountContext = createContext<AccountContextValue | null>(null);

function permissionMatches(granted: ReadonlySet<string>, permission: string) {
  if (granted.has("*") || granted.has(permission)) return true;
  const parts = permission.split(".");
  while (parts.length > 1) {
    parts.pop();
    if (granted.has(`${parts.join(".")}.*`)) return true;
  }
  return false;
}

export function AccountProvider({ children }: { children: ReactNode }) {
  const [principal, setPrincipal] = useState<WebUiPrincipal | null>(null);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const next = await getCurrentAccount();
      setPrincipal(next);
      return next;
    } catch (error) {
      if (error instanceof WebUiApiError && error.status === 401) {
        setPrincipal(null);
        return null;
      }
      setPrincipal(null);
      throw error;
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    void getCurrentAccount()
      .then((next) => {
        if (!cancelled) setPrincipal(next);
      })
      .catch(() => {
        if (!cancelled) setPrincipal(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const login = useCallback(async (payload: { login_name: string; password: string }) => {
    const next = await loginWebUiAccount(payload);
    setPrincipal(next);
    setLoading(false);
    return next;
  }, []);

  const register = useCallback(async (payload: {
    login_name: string;
    display_name: string;
    password: string;
  }) => {
    const next = await registerAccount(payload);
    setPrincipal(next);
    setLoading(false);
    return next;
  }, []);

  const logout = useCallback(async () => {
    try {
      await logoutWebUiAccount();
    } finally {
      setPrincipal(null);
      setLoading(false);
    }
  }, []);

  const permissions = useMemo(
    () => new Set(principal?.permissions ?? []),
    [principal?.permissions],
  );
  const can = useCallback(
    (permission: string) => Boolean(principal) && permissionMatches(permissions, permission),
    [permissions, principal],
  );
  const canAny = useCallback(
    (required: readonly string[]) => required.some(can),
    [can],
  );

  const value = useMemo<AccountContextValue>(
    () => ({
      principal,
      account: principal?.account ?? null,
      permissions,
      authenticated: Boolean(principal),
      loading,
      can,
      canAny,
      login,
      register,
      logout,
      refresh,
    }),
    [principal, permissions, loading, can, canAny, login, register, logout, refresh],
  );

  return <AccountContext.Provider value={value}>{children}</AccountContext.Provider>;
}

export function useAccount() {
  const value = useContext(AccountContext);
  if (!value) throw new Error("useAccount 必须在 AccountProvider 中使用");
  return value;
}
