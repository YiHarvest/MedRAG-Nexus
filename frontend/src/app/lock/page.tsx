import { Locked } from "@carbon/icons-react";
import { LockForm } from "./lock-form";
import styles from "./lock-page.module.css";
import { safeWebUiReturnPath } from "@/server/auth/webui-lock";

export const dynamic = "force-dynamic";

interface LockPageProps {
  searchParams: Promise<{ error?: string; next?: string }>;
}

export default async function LockPage({ searchParams }: LockPageProps) {
  const query = await searchParams;
  const returnPath = safeWebUiReturnPath(query.next);

  return (
    <main className={styles.page}>
      <section className={styles.panel} aria-labelledby="lock-title">
        <div className={styles.icon} aria-hidden="true">
          <Locked size={24} />
        </div>
        <div className={styles.heading}>
          <h1 id="lock-title">知识库已锁定</h1>
          <p>请输入访问密码继续使用管理页面。</p>
        </div>
        <LockForm
          hasError={query.error === "invalid"}
          returnPath={returnPath}
        />
        <p className={styles.note}>
          解锁仅在当前浏览器会话有效，关闭浏览器后再次访问需要重新输入。
        </p>
      </section>
    </main>
  );
}
