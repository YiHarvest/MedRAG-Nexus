import type { RecentTask } from "@/lib/types";

const RECENT_TASKS_KEY = "jd-knowledge-recent-tasks";

export function readRecentTasks(): RecentTask[] {
  if (typeof window === "undefined") return [];
  try {
    const parsed = JSON.parse(window.localStorage.getItem(RECENT_TASKS_KEY) ?? "[]") as unknown;
    return Array.isArray(parsed) ? (parsed as RecentTask[]).slice(0, 12) : [];
  } catch {
    return [];
  }
}

export function rememberTask(task: RecentTask): void {
  const next = [task, ...readRecentTasks().filter((item) => item.taskId !== task.taskId)].slice(0, 12);
  window.localStorage.setItem(RECENT_TASKS_KEY, JSON.stringify(next));
  window.dispatchEvent(new CustomEvent("jd-knowledge-task-added"));
}

export function formatDate(value?: string | null): string {
  if (!value) return "暂无";
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

export function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("读取文件失败"));
    reader.onload = () => {
      const result = String(reader.result ?? "");
      resolve(result.includes(",") ? result.split(",", 2)[1] : result);
    };
    reader.readAsDataURL(file);
  });
}
