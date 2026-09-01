import { NextResponse } from "next/server";

const WEBUI_LOCK_PURPOSE = "jd-knowledge:webui-session:v1";

export const WEBUI_LOCK_COOKIE_NAME = "jd_knowledge_webui_session_v2";
export const WEBUI_LOCK_SESSION_SECONDS = 12 * 60 * 60;

type WebUiLockEnvironment = Record<string, string | undefined>;

export interface WebUiLockConfig {
  enabled: boolean;
  password?: string;
}

export interface WebUiTrustedProxyConfig {
  trustHeaders: boolean;
  trustedHops: number;
}

export function readWebUiLockConfig(
  env: WebUiLockEnvironment = process.env,
): WebUiLockConfig {
  const password = env.WEBUI_LOCK_PASSWORD?.trim() || undefined;

  if (env.NODE_ENV === "production" && !password) {
    throw new Error("生产环境必须配置 WEBUI_LOCK_PASSWORD。\n");
  }

  return { enabled: Boolean(password), password };
}

export function readWebUiTrustedProxyConfig(
  env: WebUiLockEnvironment = process.env,
): WebUiTrustedProxyConfig {
  const rawTrust = env.WEBUI_TRUST_PROXY_HEADERS?.trim().toLowerCase();
  if (rawTrust && !["1", "true", "yes", "0", "false", "no"].includes(rawTrust)) {
    throw new Error("WEBUI_TRUST_PROXY_HEADERS 必须是 true 或 false。");
  }
  const trustHeaders = ["1", "true", "yes"].includes(rawTrust ?? "");
  const rawHops = env.WEBUI_TRUSTED_PROXY_HOPS?.trim() || "1";
  const trustedHops = Number(rawHops);
  if (!Number.isSafeInteger(trustedHops) || trustedHops < 1 || trustedHops > 16) {
    throw new Error("WEBUI_TRUSTED_PROXY_HOPS 必须是 1 到 16 之间的整数。");
  }
  return { trustHeaders, trustedHops };
}

export function isProtectedWebUiPath(pathname: string) {
  const normalized = normalizePathname(pathname);
  if (
    normalized === "/backend/api/webui" ||
    normalized.startsWith("/backend/api/webui/")
  ) {
    return true;
  }
  return !isPublicInfrastructurePath(normalized) && normalized !== "/lock";
}

export function safeWebUiReturnPath(value: string | null | undefined) {
  const fallback = "/documents";
  if (!value || !value.startsWith("/") || value.startsWith("//")) {
    return fallback;
  }

  let url: URL;
  try {
    url = new URL(value, "http://webui.local");
  } catch {
    return fallback;
  }

  if (url.origin !== "http://webui.local") return fallback;
  if (!isProtectedWebUiPath(url.pathname)) return fallback;
  return `${url.pathname}${url.search}`;
}

const WEBUI_REDIRECT_BASE = "http://webui.local";

export function webUiRedirectPath(
  path: string,
  query?: Record<string, string>,
) {
  const url = new URL(path, WEBUI_REDIRECT_BASE);
  for (const [key, value] of Object.entries(query ?? {})) {
    url.searchParams.set(key, value);
  }
  return `${url.pathname}${url.search}`;
}

export function webUiRedirectResponse(
  path: string,
  query?: Record<string, string>,
) {
  return new NextResponse(null, {
    status: 303,
    headers: {
      location: webUiRedirectPath(path, query),
      "cache-control": "no-store",
    },
  });
}

export async function createWebUiSession(password: string, now = Date.now()) {
  const expiresAt = Math.floor(now / 1000) + WEBUI_LOCK_SESSION_SECONDS;
  const nonce = crypto.getRandomValues(new Uint8Array(18));
  const payload = `v1.${expiresAt}.${encodeBase64Url(nonce)}`;
  const signature = await sign(payload, password);
  return `${payload}.${encodeBase64Url(signature)}`;
}

export async function verifyWebUiSession(
  value: string | null | undefined,
  password: string,
  now = Date.now(),
) {
  if (!value) return false;
  const parts = value.split(".");
  if (parts.length !== 4 || parts[0] !== "v1") return false;

  const expiresAt = Number(parts[1]);
  if (!Number.isSafeInteger(expiresAt) || expiresAt <= Math.floor(now / 1000)) {
    return false;
  }

  try {
    const signingKey = await deriveSigningKey(password);
    return crypto.subtle.verify(
      "HMAC",
      signingKey,
      decodeBase64Url(parts[3]),
      new TextEncoder().encode(parts.slice(0, 3).join(".")),
    );
  } catch {
    return false;
  }
}

function isPublicInfrastructurePath(pathname: string) {
  return (
    pathname === "/api" ||
    pathname.startsWith("/api/") ||
    pathname === "/backend" ||
    pathname.startsWith("/backend/") ||
    pathname === "/mcp" ||
    pathname.startsWith("/mcp/") ||
    pathname === "/_next" ||
    pathname.startsWith("/_next/") ||
    pathname === "/favicon.ico" ||
    pathname === "/icon.svg"
  );
}

function normalizePathname(value: string) {
  if (value === "/") return value;
  return value.replace(/\/+$/, "") || "/";
}

async function sign(payload: string, password: string) {
  const signingKey = await deriveSigningKey(password);
  return new Uint8Array(
    await crypto.subtle.sign(
      "HMAC",
      signingKey,
      new TextEncoder().encode(payload),
    ),
  );
}

async function deriveSigningKey(password: string) {
  const sourceKey = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(password),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const derived = await crypto.subtle.sign(
    "HMAC",
    sourceKey,
    new TextEncoder().encode(WEBUI_LOCK_PURPOSE),
  );
  return crypto.subtle.importKey(
    "raw",
    derived,
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign", "verify"],
  );
}

function encodeBase64Url(value: Uint8Array) {
  let binary = "";
  for (const byte of value) binary += String.fromCharCode(byte);
  return btoa(binary)
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/g, "");
}

function decodeBase64Url(value: string) {
  if (!/^[A-Za-z0-9_-]+$/.test(value)) {
    throw new Error("Invalid base64url value");
  }
  const normalized = value.replace(/-/g, "+").replace(/_/g, "/");
  const binary = atob(
    normalized.padEnd(Math.ceil(normalized.length / 4) * 4, "="),
  );
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}
