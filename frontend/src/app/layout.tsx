import type { Metadata } from "next";
import "@carbon/styles/css/styles.css";
import "./globals.css";
import { ShellBoundary } from "@/components/shell-boundary";

export const metadata: Metadata = {
  title: {
    default: "JD Knowledge",
    template: "%s | JD Knowledge",
  },
  description: "知识文档入库、检索与任务运维控制台",
  applicationName: "JD Knowledge",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN" data-theme="white" suppressHydrationWarning>
      <head>
        <script
          dangerouslySetInnerHTML={{
            __html: `(() => { try { const mode = localStorage.getItem('jd-knowledge-theme') || 'system'; const dark = mode === 'dark' || (mode === 'system' && matchMedia('(prefers-color-scheme: dark)').matches); document.documentElement.dataset.theme = dark ? 'g100' : 'white'; } catch {} })()`,
          }}
        />
      </head>
      <body>
        <ShellBoundary>{children}</ShellBoundary>
      </body>
    </html>
  );
}
