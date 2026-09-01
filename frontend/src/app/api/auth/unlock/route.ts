import { isIP } from "node:net";
import { NextResponse } from "next/server";
import {
  createWebUiSession,
  readWebUiLockConfig,
  readWebUiTrustedProxyConfig,
  safeWebUiReturnPath,
  webUiRedirectResponse,
  WEBUI_LOCK_COOKIE_NAME,
} from "@/server/auth/webui-lock";
import { webUiPasswordMatches } from "@/server/auth/webui-lock-node";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const MAX_BODY_BYTES = 4_096;
const MAX_ATTEMPTS = 5;
const ATTEMPT_WINDOW_MS = 5 * 60 * 1_000;
const rateLimiter = createInMemoryRateLimiter(MAX_ATTEMPTS, ATTEMPT_WINDOW_MS);

export async function POST(request: Request) {
  let config;
  let proxyConfig;
  try {
    config = readWebUiLockConfig();
    proxyConfig = readWebUiTrustedProxyConfig();
  } catch {
    return unavailable();
  }

  if (!config.enabled || !config.password) {
    return webUiRedirectResponse("/documents");
  }

  const contentType = request.headers.get("content-type") || "";
  const contentLength = Number(request.headers.get("content-length") || 0);
  if (!contentType.startsWith("application/x-www-form-urlencoded")) {
    return new NextResponse("Unsupported Media Type", { status: 415 });
  }
  if (contentLength > MAX_BODY_BYTES) {
    return new NextResponse("Payload Too Large", { status: 413 });
  }

  const body = await request.text();
  if (new TextEncoder().encode(body).byteLength > MAX_BODY_BYTES) {
    return new NextResponse("Payload Too Large", { status: 413 });
  }

  const client = clientAddress(request, proxyConfig.trustHeaders, proxyConfig.trustedHops);
  const now = Date.now();
  if (rateLimiter.isRateLimited(client, now)) {
    return new NextResponse("Too Many Requests", {
      status: 429,
      headers: { "retry-after": "300", "cache-control": "no-store" },
    });
  }

  const form = new URLSearchParams(body);
  const returnPath = safeWebUiReturnPath(form.get("next"));
  if (!webUiPasswordMatches(form.get("password"), config.password)) {
    rateLimiter.recordFailure(client, now);
    return webUiRedirectResponse("/lock", {
      error: "invalid",
      next: returnPath,
    });
  }

  rateLimiter.reset(client);
  const session = await createWebUiSession(config.password, now);
  const response = webUiRedirectResponse(returnPath);
  response.cookies.set(WEBUI_LOCK_COOKIE_NAME, session, {
    httpOnly: true,
    secure: isSecureRequest(request, proxyConfig.trustHeaders, proxyConfig.trustedHops),
    sameSite: "lax",
    path: "/",
  });
  return response;
}

function clientAddress(request: Request, trustProxyHeaders: boolean, trustedProxyHops: number) {
  if (!trustProxyHeaders) return "untrusted-direct-client";
  const forwarded = trustedForwardedValue(request, "x-forwarded-for", trustedProxyHops);
  if (forwarded && isIP(forwarded)) return forwarded;
  for (const header of ["x-real-ip", "cf-connecting-ip", "x-client-ip"]) {
    const value = request.headers.get(header)?.trim();
    if (value && isIP(value)) return value;
  }
  return "trusted-proxy-client-unknown";
}

function isSecureRequest(request: Request, trustProxyHeaders: boolean, trustedProxyHops: number) {
  const forwardedProtocol = trustProxyHeaders
    ? trustedForwardedValue(request, "x-forwarded-proto", trustedProxyHops)?.toLowerCase()
    : undefined;
  return forwardedProtocol === "https" || new URL(request.url).protocol === "https:";
}

function trustedForwardedValue(request: Request, header: string, trustedProxyHops: number) {
  const values = request.headers.get(header)?.split(",").map((value) => value.trim()).filter(Boolean) ?? [];
  return values.at(-trustedProxyHops);
}

function createInMemoryRateLimiter(maxAttempts: number, windowMs: number) {
  const attempts = new Map<string, { count: number; resetAt: number }>();
  return {
    isRateLimited(client: string, now: number) {
      const entry = attempts.get(client);
      if (!entry || entry.resetAt <= now) {
        attempts.delete(client);
        return false;
      }
      return entry.count >= maxAttempts;
    },
    recordFailure(client: string, now: number) {
      const entry = attempts.get(client);
      if (!entry || entry.resetAt <= now) {
        attempts.set(client, { count: 1, resetAt: now + windowMs });
        return;
      }
      entry.count += 1;
    },
    reset(client: string) {
      attempts.delete(client);
    },
  };
}

function unavailable() {
  return new NextResponse("WebUI lock configuration is invalid.", {
    status: 503,
    headers: { "cache-control": "no-store" },
  });
}
