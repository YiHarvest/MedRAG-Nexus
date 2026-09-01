"use client";

import {
  Activity,
  Chat,
  ChevronLeft,
  ChevronRight,
  Close,
  Document,
  Laptop,
  Menu,
  Moon,
  Search,
  Settings,
  SidePanelClose,
  SidePanelOpen,
  Sun,
  User,
} from "@carbon/icons-react";
import { Theme } from "@carbon/react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { useAccount } from "@/components/account";
import { ShellHeaderActionContext, type ShellHeaderAction } from "@/components/shell-header-action";
import { useWorkspace } from "@/components/workspace";
import { PAGE_PERMISSIONS } from "@/lib/webui-permissions";

const navigation = [
  { href: "/documents", label: "文档", detail: "入库与维护", icon: Document, capability: "documents" },
  { href: "/retrieval", label: "检索", detail: "混合召回", icon: Search, capability: "retrieval" },
  { href: "/chat", label: "聊天", detail: "知识问答", icon: Chat, capability: "chat" },
  { href: "/management", label: "管理", detail: "账号与知识库", icon: Settings, capability: "management" },
  { href: "/system", label: "系统", detail: "运行状态", icon: Activity, capability: "system" },
] as const;

type CarbonTheme = "white" | "g100";
type ThemeMode = "light" | "dark" | "system";

const themeOptions = [
  { mode: "light" as const, label: "浅色", icon: Sun },
  { mode: "dark" as const, label: "暗色", icon: Moon },
  { mode: "system" as const, label: "跟随系统", icon: Laptop },
];

export function AppShell({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const { authenticated, hydrated } = useWorkspace();
  const { account, canAny } = useAccount();
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [themeMenuOpen, setThemeMenuOpen] = useState(false);
  const [mode, setMode] = useState<ThemeMode>("system");
  const [theme, setTheme] = useState<CarbonTheme>("white");
  const [headerAction, setHeaderAction] = useState<ShellHeaderAction | null>(null);
  const mobileCloseRef = useRef<HTMLButtonElement>(null);
  const mobileTriggerRef = useRef<HTMLButtonElement>(null);
  const registerHeaderAction = useCallback((action: ShellHeaderAction | null) => {
    setHeaderAction(action);
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      const saved = window.localStorage.getItem("medrag-nexus-theme");
      const next = saved === "light" || saved === "dark" || saved === "system"
        ? saved
        : "system";
      setMode(next);
    }, 0);
    return () => window.clearTimeout(timer);
  }, []);

  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const syncTheme = () => {
      const isDark = mode === "dark" || (mode === "system" && media.matches);
      const nextTheme: CarbonTheme = isDark ? "g100" : "white";
      setTheme(nextTheme);
      document.documentElement.dataset.theme = nextTheme;
      window.localStorage.setItem("medrag-nexus-theme", mode);
    };
    syncTheme();
    if (mode !== "system") return;
    media.addEventListener("change", syncTheme);
    return () => media.removeEventListener("change", syncTheme);
  }, [mode]);

  useEffect(() => {
    if (!mobileOpen) return;
    const trigger = mobileTriggerRef.current;
    const previousOverflow = document.body.style.overflow;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setMobileOpen(false);
    };
    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", closeOnEscape);
    const timer = window.setTimeout(() => mobileCloseRef.current?.focus(), 0);
    return () => {
      window.clearTimeout(timer);
      window.removeEventListener("keydown", closeOnEscape);
      document.body.style.overflow = previousOverflow;
      trigger?.focus();
    };
  }, [mobileOpen]);

  const visibleNavigation = navigation.filter((item) => canAny(PAGE_PERMISSIONS[item.capability]));
  const currentNavigation = navigation.find((item) => pathname === item.href);
  const activeNavigation = pathname === "/login"
    ? { label: "账号", detail: authenticated ? "登录信息" : "登录或注册" }
    : currentNavigation ?? visibleNavigation[0] ?? { label: "工作台", detail: "暂无可用功能" };
  const authorizedForPath = !currentNavigation || canAny(PAGE_PERMISSIONS[currentNavigation.capability]);
  const selectedTheme = themeOptions.find((option) => option.mode === mode) ?? themeOptions[2];
  const SelectedThemeIcon = selectedTheme.icon;

  const sidebarContent = (mobile = false) => (
    <div className="global-sidebar-inner">
      <div className="sidebar-header">
        <Link
          className="brand"
          href="/documents"
          aria-label="MedRAG-Nexus 首页"
          onClick={() => setMobileOpen(false)}
        >
          <span className="brand-mark" aria-hidden="true"><span>JD</span></span>
          <span className="brand-copy"><strong>MedRAG-Nexus</strong></span>
        </Link>
        {mobile ? (
          <button
            ref={mobileCloseRef}
            className="sidebar-icon-button"
            type="button"
            aria-label="关闭导航"
            onClick={() => setMobileOpen(false)}
          >
            <Close size={18} />
          </button>
        ) : null}
      </div>

      <nav className="sidebar-nav" aria-label={mobile ? "移动端主导航" : "主导航"}>
        {visibleNavigation.map((item) => {
          const Icon = item.icon;
          return (
            <Link
              className="sidebar-nav-link"
              data-active={pathname === item.href}
              href={item.href}
              key={item.href}
              onClick={() => setMobileOpen(false)}
            >
              <Icon size={16} aria-hidden="true" />
              <span>{item.label}</span>
              {mobile ? <small>{item.detail}</small> : null}
            </Link>
          );
        })}
      </nav>

      <div className="sidebar-footer">
        <Link
          className="sidebar-account"
          data-active={pathname === "/login"}
          href="/login"
          onClick={() => setMobileOpen(false)}
        >
          <span className="sidebar-account-icon"><User size={16} /></span>
          <span>
            <strong>{account ? account.display_name || account.login_name : "登录"}</strong>
            <small>{account ? `等级 ${account.permission_level} · 账号信息` : "用户名和密码"}</small>
          </span>
        </Link>
        <div className="theme-picker sidebar-theme-picker">
          <button
            className="sidebar-theme-button"
            type="button"
            aria-label={`主题：${selectedTheme.label}`}
            aria-haspopup="menu"
            aria-expanded={themeMenuOpen}
            onClick={() => setThemeMenuOpen((current) => !current)}
          >
            <SelectedThemeIcon size={16} />
            <span>外观</span>
            <small>{selectedTheme.label}</small>
          </button>
          {themeMenuOpen ? (
            <div className="theme-menu sidebar-theme-menu" role="menu" aria-label="选择主题">
              {themeOptions.map((option) => {
                const ThemeIcon = option.icon;
                return (
                  <button
                    className="theme-option"
                    data-selected={mode === option.mode}
                    key={option.mode}
                    role="menuitemradio"
                    aria-checked={mode === option.mode}
                    type="button"
                    onClick={() => {
                      setMode(option.mode);
                      setThemeMenuOpen(false);
                    }}
                  >
                    <ThemeIcon size={16} />
                    {option.label}
                  </button>
                );
              })}
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );

  return (
    <Theme theme={theme} className="app-shell">
      <ShellHeaderActionContext.Provider value={registerHeaderAction}>
        <a className="skip-link" href="#main-content">跳到主要内容</a>
        <div className="shell-layout">
          <aside
            className="global-sidebar"
            data-open={sidebarOpen}
            aria-label="应用导航"
            aria-hidden={!sidebarOpen}
            inert={!sidebarOpen}
          >
            {sidebarContent()}
          </aside>

          <div className="shell-content">
            <header className="content-header">
              <button
                className="sidebar-toggle sidebar-toggle-desktop"
                type="button"
                aria-label={sidebarOpen ? "收起侧栏" : "展开侧栏"}
                aria-expanded={sidebarOpen}
                onClick={() => setSidebarOpen((current) => !current)}
              >
                {sidebarOpen ? <SidePanelClose size={18} /> : <SidePanelOpen size={18} />}
              </button>
              <button
                ref={mobileTriggerRef}
                className="sidebar-toggle sidebar-toggle-mobile"
                type="button"
                aria-label="打开导航"
                aria-expanded={mobileOpen}
                aria-controls="mobile-sidebar"
                onClick={() => setMobileOpen(true)}
              >
                <Menu size={19} />
              </button>
              <div className="shell-context">
                <strong>{activeNavigation.label}</strong>
                <span>{activeNavigation.detail}</span>
              </div>
              {authenticated && pathname !== "/management" ? (
                <Link className="content-header-management" href="/management">
                  <Settings size={16} aria-hidden="true" />
                  <span>管理</span>
                </Link>
              ) : null}
              {headerAction ? (
                <button
                  className="content-header-action"
                  type="button"
                  aria-controls={headerAction.controls}
                  aria-expanded={headerAction.expanded}
                  onClick={headerAction.onClick}
                >
                  <span>{headerAction.label}</span>
                  {headerAction.expanded ? <ChevronRight size={16} /> : <ChevronLeft size={16} />}
                </button>
              ) : null}
            </header>

            <main className="app-main" id="main-content">
              {pathname === "/login" ? children : !hydrated ? (
                <div className="session-state" role="status">正在恢复用户状态…</div>
              ) : authenticated && account?.must_change_password ? (
                <section className="session-required">
                  <span><User size={24} /></span>
                  <h1>需要修改密码</h1>
                  <p>当前账号使用的是初始密码，请先设置新密码后继续。</p>
                  <Link href="/login">前往修改密码</Link>
                </section>
              ) : authenticated && !authorizedForPath ? (
                <section className="session-required">
                  <span><User size={24} /></span>
                  <h1>没有访问权限</h1>
                  <p>当前账号无权使用此功能。请联系管理员调整等级或权限组。</p>
                  <Link href={visibleNavigation[0]?.href ?? "/login"}>返回可用页面</Link>
                </section>
              ) : authenticated ? children : (
                <section className="session-required">
                  <span><User size={24} /></span>
                  <h1>请先登录</h1>
                  <p>请使用用户名和密码登录后继续。</p>
                  <Link href="/login">前往登录</Link>
                </section>
              )}
            </main>
          </div>
        </div>

        {mobileOpen ? (
          <button
            type="button"
            className="mobile-sidebar-backdrop"
            aria-label="关闭导航"
            onClick={() => setMobileOpen(false)}
          />
        ) : null}
        <aside
          className="mobile-sidebar"
          id="mobile-sidebar"
          data-open={mobileOpen}
          role="dialog"
          aria-modal="true"
          aria-label="应用导航"
          inert={!mobileOpen}
        >
          {sidebarContent(true)}
        </aside>
      </ShellHeaderActionContext.Provider>
    </Theme>
  );
}
