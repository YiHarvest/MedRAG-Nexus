"use client";

import { createContext, useContext, useEffect } from "react";

export interface ShellHeaderAction {
  label: string;
  controls?: string;
  expanded?: boolean;
  onClick: () => void;
}

type RegisterShellHeaderAction = (action: ShellHeaderAction | null) => void;

export const ShellHeaderActionContext = createContext<RegisterShellHeaderAction | null>(null);

export function useShellHeaderAction(action: ShellHeaderAction | null) {
  const register = useContext(ShellHeaderActionContext);

  useEffect(() => {
    if (!register) return;
    register(action);
    return () => register(null);
  }, [action, register]);
}
