"use client";

import { ArrowRight, User } from "@carbon/icons-react";
import { Button, InlineNotification, PasswordInput, TextInput } from "@carbon/react";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { useAccount } from "@/components/account";
import { changeOwnWebUiPassword } from "@/lib/webui-admin-api";
import { defaultAccountRoute } from "@/lib/webui-permissions";
import { describeWebUiError } from "@/lib/webui-api";
import styles from "./login-view.module.css";

type AuthMode = "login" | "register";

const LEVEL_LABELS: Record<number, string> = {
  0: "初级用户",
  1: "VIP 用户",
  2: "知识库管理员",
  1000: "超级管理员",
};

export function LoginView() {
  const router = useRouter();
  const { account, principal, loading, can, login, register, logout } = useAccount();
  const [mode, setMode] = useState<AuthMode>("login");
  const [loginName, setLoginName] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [passwordConfirm, setPasswordConfirm] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [newPasswordConfirm, setNewPasswordConfirm] = useState("");
  const [passwordBusy, setPasswordBusy] = useState(false);

  async function submit() {
    const normalizedLoginName = loginName.trim();
    if (!normalizedLoginName || !password) return;
    if (mode === "register" && password !== passwordConfirm) {
      setError("两次输入的密码不一致。");
      return;
    }
    if (mode === "register" && password.length < 3) {
      setError("密码至少需要 3 个字符。");
      return;
    }

    setSubmitting(true);
    setError(null);
    try {
      const next = mode === "login"
        ? await login({ login_name: normalizedLoginName, password })
        : await register({
          login_name: normalizedLoginName,
          display_name: displayName.trim() || normalizedLoginName,
          password,
        });
      router.replace(defaultAccountRoute(next));
      router.refresh();
    } catch (reason) {
      setError(describeWebUiError(reason));
    } finally {
      setSubmitting(false);
    }
  }

  async function submitPasswordChange() {
    if (!can("webui.account.password.change_self")) return;
    if (newPassword.length < 3) {
      setError("新密码至少需要 3 个字符。");
      return;
    }
    if (newPassword !== newPasswordConfirm) {
      setError("两次输入的新密码不一致。");
      return;
    }

    setPasswordBusy(true);
    setError(null);
    setSuccess(null);
    try {
      await changeOwnWebUiPassword({
        current_password: currentPassword,
        new_password: newPassword,
      });
      setCurrentPassword("");
      setNewPassword("");
      setNewPasswordConfirm("");
      await logout();
      setSuccess("密码已修改，原有会话已撤销，请使用新密码重新登录。");
      router.replace("/login");
    } catch (reason) {
      setError(describeWebUiError(reason));
    } finally {
      setPasswordBusy(false);
    }
  }

  if (loading) {
    return <div className="session-state" role="status">正在验证登录状态…</div>;
  }

  if (account && principal) {
    const levelLabel = LEVEL_LABELS[account.permission_level] ?? "自定义等级";
    return (
      <div className="page login-page">
        <section className="login-panel" aria-labelledby="account-title">
          <div className="login-heading">
            <span className="login-icon"><User size={24} /></span>
            <div>
              <h1 id="account-title">{account.display_name || account.login_name}</h1>
              <p>当前已使用服务端会话登录，页面不会信任浏览器保存的用户身份。</p>
            </div>
          </div>

          <dl className="account-summary">
            <div><dt>登录名</dt><dd>{account.login_name}</dd></div>
            <div>
              <dt>成员等级</dt>
              <dd className={styles.readableValue}>
                <span>{levelLabel}</span>
                <code>（{account.permission_level}）</code>
              </dd>
            </div>
            <div>
              <dt>权限组</dt>
              <dd className={styles.groupValues}>
                {account.groups.length ? account.groups.map((group) => (
                  <span className={styles.readableValue} key={group}>
                    <span>组织权限组</span>
                    <code>（{group}）</code>
                  </span>
                )) : <span>默认权限</span>}
              </dd>
            </div>
          </dl>

          {can("webui.account.password.change_self") ? (
            <form
              className={styles.passwordPanel}
              onSubmit={(event) => {
                event.preventDefault();
                void submitPasswordChange();
              }}
            >
              <div className={styles.passwordHeading}>
                <h2>修改密码</h2>
                <p>修改成功后会撤销全部登录会话，需要使用新密码重新登录。</p>
              </div>
              <PasswordInput
                id="account-current-password"
                labelText="当前密码"
                value={currentPassword}
                autoComplete="current-password"
                required
                disabled={passwordBusy}
                showPasswordLabel="显示密码"
                hidePasswordLabel="隐藏密码"
                onChange={(event) => setCurrentPassword(event.target.value)}
              />
              <PasswordInput
                id="account-new-password"
                labelText="新密码"
                value={newPassword}
                autoComplete="new-password"
                minLength={3}
                helperText="至少 3 个字符"
                required
                disabled={passwordBusy}
                showPasswordLabel="显示密码"
                hidePasswordLabel="隐藏密码"
                onChange={(event) => setNewPassword(event.target.value)}
              />
              <PasswordInput
                id="account-new-password-confirm"
                labelText="确认新密码"
                value={newPasswordConfirm}
                autoComplete="new-password"
                minLength={3}
                required
                disabled={passwordBusy}
                showPasswordLabel="显示密码"
                hidePasswordLabel="隐藏密码"
                onChange={(event) => setNewPasswordConfirm(event.target.value)}
              />
              <Button
                type="submit"
                kind="secondary"
                disabled={!currentPassword || !newPassword || !newPasswordConfirm || passwordBusy}
              >
                {passwordBusy ? "正在修改…" : "修改密码"}
              </Button>
            </form>
          ) : null}

          <div className="login-actions">
            <Button
              renderIcon={ArrowRight}
              onClick={() => router.push(defaultAccountRoute(principal))}
            >
              返回工作台
            </Button>
            <Button
              kind="danger--ghost"
              disabled={submitting}
              onClick={() => {
                setSubmitting(true);
                void logout()
                  .then(() => router.replace("/login"))
                  .catch((reason) => setError(describeWebUiError(reason)))
                  .finally(() => setSubmitting(false));
              }}
            >
              {submitting ? "正在退出…" : "退出账号"}
            </Button>
          </div>
          {error ? (
            <InlineNotification
              kind="error"
              title="操作失败"
              subtitle={error}
              lowContrast
              onCloseButtonClick={() => setError(null)}
            />
          ) : null}
        </section>
      </div>
    );
  }

  return (
    <div className="page login-page">
      <section className="login-panel" aria-labelledby="login-title">
        <div className="login-heading">
          <span className="login-icon"><User size={24} /></span>
          <div>
            <h1 id="login-title">账号登录</h1>
            <p>登录后只展示当前账号有权查看和操作的知识库。</p>
          </div>
        </div>

        {success ? (
          <InlineNotification
            kind="success"
            title="密码修改成功"
            subtitle={success}
            lowContrast
            onCloseButtonClick={() => setSuccess(null)}
          />
        ) : null}

        <div className="login-mode-switch" role="tablist" aria-label="账号入口">
          <button
            type="button"
            role="tab"
            aria-selected={mode === "login"}
            data-selected={mode === "login"}
            onClick={() => {
              setMode("login");
              setError(null);
            }}
          >登录</button>
          <button
            type="button"
            role="tab"
            aria-selected={mode === "register"}
            data-selected={mode === "register"}
            onClick={() => {
              setMode("register");
              setError(null);
            }}
          >注册</button>
        </div>

        {error ? (
          <InlineNotification
            kind="error"
            title={mode === "login" ? "登录失败" : "注册失败"}
            subtitle={error}
            lowContrast
            onCloseButtonClick={() => setError(null)}
          />
        ) : null}

        <form
          className="account-login-form"
          onSubmit={(event) => {
            event.preventDefault();
            void submit();
          }}
        >
          <TextInput
            id="account-login-name"
            labelText="用户名"
            value={loginName}
            autoComplete="username"
            maxLength={128}
            required
            autoFocus
            disabled={submitting}
            onChange={(event) => setLoginName(event.target.value)}
          />
          {mode === "register" ? (
            <TextInput
              id="account-display-name"
              labelText="显示名称"
              value={displayName}
              autoComplete="name"
              maxLength={128}
              disabled={submitting}
              placeholder="不填写时使用用户名"
              onChange={(event) => setDisplayName(event.target.value)}
            />
          ) : null}
          <PasswordInput
            id="account-password"
            labelText="密码"
            value={password}
            autoComplete={mode === "login" ? "current-password" : "new-password"}
            minLength={mode === "register" ? 3 : undefined}
            helperText={mode === "register" ? "至少 3 个字符" : undefined}
            required
            disabled={submitting}
            showPasswordLabel="显示密码"
            hidePasswordLabel="隐藏密码"
            onChange={(event) => setPassword(event.target.value)}
          />
          {mode === "register" ? (
            <PasswordInput
              id="account-password-confirm"
              labelText="确认密码"
              value={passwordConfirm}
              autoComplete="new-password"
              minLength={3}
              required
              disabled={submitting}
              showPasswordLabel="显示密码"
              hidePasswordLabel="隐藏密码"
              onChange={(event) => setPasswordConfirm(event.target.value)}
            />
          ) : null}
          <Button
            type="submit"
            size="lg"
            renderIcon={ArrowRight}
            disabled={!loginName.trim() || !password || submitting}
          >
            {submitting ? "正在提交…" : mode === "login" ? "登录" : "创建账号"}
          </Button>
        </form>
      </section>
    </div>
  );
}
