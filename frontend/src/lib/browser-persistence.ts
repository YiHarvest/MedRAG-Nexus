const DATABASE_NAME = "jd-knowledge-browser-state";
const DATABASE_VERSION = 1;
const STORE_NAME = "values";

function openDatabase(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = window.indexedDB.open(DATABASE_NAME, DATABASE_VERSION);
    request.onupgradeneeded = () => {
      const database = request.result;
      if (!database.objectStoreNames.contains(STORE_NAME)) database.createObjectStore(STORE_NAME);
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("无法打开浏览器持久化存储"));
  });
}

export async function readBrowserValue<T>(key: string): Promise<T | null> {
  const database = await openDatabase();
  try {
    return await new Promise<T | null>((resolve, reject) => {
      const transaction = database.transaction(STORE_NAME, "readonly");
      const request = transaction.objectStore(STORE_NAME).get(key);
      request.onsuccess = () => resolve((request.result as T | undefined) ?? null);
      request.onerror = () => reject(request.error ?? new Error("无法读取浏览器持久化数据"));
    });
  } finally {
    database.close();
  }
}

export async function writeBrowserValue<T>(key: string, value: T): Promise<void> {
  const database = await openDatabase();
  try {
    await new Promise<void>((resolve, reject) => {
      const transaction = database.transaction(STORE_NAME, "readwrite");
      transaction.objectStore(STORE_NAME).put(value, key);
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => reject(transaction.error ?? new Error("无法保存浏览器持久化数据"));
      transaction.onabort = () => reject(transaction.error ?? new Error("浏览器持久化写入已中止"));
    });
  } finally {
    database.close();
  }
}

export async function removeBrowserValue(key: string): Promise<void> {
  const database = await openDatabase();
  try {
    await new Promise<void>((resolve, reject) => {
      const transaction = database.transaction(STORE_NAME, "readwrite");
      transaction.objectStore(STORE_NAME).delete(key);
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => reject(transaction.error ?? new Error("无法删除浏览器持久化数据"));
      transaction.onabort = () => reject(transaction.error ?? new Error("浏览器持久化删除已中止"));
    });
  } finally {
    database.close();
  }
}

export interface PersistedUploadFile {
  localId: string;
  file: File;
}

function uploadFilesKey(userId: string): string {
  return `upload-files:${userId}`;
}

export async function saveUploadFiles(userId: string, files: PersistedUploadFile[]): Promise<void> {
  await writeBrowserValue(uploadFilesKey(userId), files);
}

export async function readUploadFiles(userId: string): Promise<PersistedUploadFile[]> {
  return (await readBrowserValue<PersistedUploadFile[]>(uploadFilesKey(userId))) ?? [];
}

export async function removeUploadFile(userId: string, localId: string): Promise<void> {
  const files = await readUploadFiles(userId);
  const remaining = files.filter((item) => item.localId !== localId);
  if (remaining.length) await saveUploadFiles(userId, remaining);
  else await removeBrowserValue(uploadFilesKey(userId));
}

export async function clearUploadFiles(userId: string): Promise<void> {
  await removeBrowserValue(uploadFilesKey(userId));
}
