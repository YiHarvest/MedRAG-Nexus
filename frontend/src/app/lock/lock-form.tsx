"use client";

import { Button, PasswordInput } from "@carbon/react";
import { Login } from "@carbon/icons-react";
import styles from "./lock-page.module.css";

interface LockFormProps {
  hasError: boolean;
  returnPath: string;
}

export function LockForm({ hasError, returnPath }: LockFormProps) {
  return (
    <form method="post" action="/api/auth/unlock" className={styles.form}>
      <input type="hidden" name="next" value={returnPath} />
      <PasswordInput
        id="webui-password"
        name="password"
        labelText="访问密码"
        placeholder="请输入访问密码"
        autoComplete="current-password"
        required
        autoFocus
        invalid={hasError}
        invalidText="访问密码不正确，请重试。"
        showPasswordLabel="显示密码"
        hidePasswordLabel="隐藏密码"
      />
      <Button
        type="submit"
        size="lg"
        renderIcon={Login}
        className={styles.submit}
      >
        解锁知识库
      </Button>
    </form>
  );
}
