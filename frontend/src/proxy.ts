import { NextRequest, NextResponse } from "next/server";
import {
  isProtectedWebUiPath,
  readWebUiLockConfig,
  readWebUiTrustedProxyConfig,
  safeWebUiReturnPath,
  verifyWebUiSession,
  WEBUI_LOCK_COOKIE_NAME,
} from "@/server/auth/webui-lock";

function firstHeaderValue(value: string | null) {
  return value?.split(",")[0]?.trim() || undefined;
}

export async function proxy(request: NextRequest) {
  let config;
  let proxyConfig;
  try {
    config = readWebUiLockConfig();
    proxyConfig = readWebUiTrustedProxyConfig();
  } catch {
    return new NextResponse("WebUI lock configuration is invalid.", {
      status: 503,
      headers: { "cache-control": "no-store" },
    });
  }

  if (!config.enabled || !config.password) return NextResponse.next();
  if (!isProtectedWebUiPath(request.nextUrl.pathname)) {
    return NextResponse.next();
  }

  const session = request.cookies.get(WEBUI_LOCK_COOKIE_NAME)?.value;
  if (await verifyWebUiSession(session, config.password)) {
    const response = NextResponse.next();
    response.headers.set("cache-control", "private, no-store");
    return response;
  }

  // 反代/绑定 0.0.0.0 部署时 nextUrl 的 host 可能是内部地址，
  // 这里按浏览器实际访问的 host 重建跳转地址（WebUI 锁只做同源跳转）。
  const host =
    (proxyConfig.trustHeaders
      ? firstHeaderValue(request.headers.get("x-forwarded-host"))
      : undefined) ||
    request.headers.get("host") ||
    request.nextUrl.host;
  const proto =
    (proxyConfig.trustHeaders
      ? firstHeaderValue(request.headers.get("x-forwarded-proto"))
      : undefined) ||
    request.nextUrl.protocol.replace(/:$/, "");
  const returnPath = safeWebUiReturnPath(
    `${request.nextUrl.pathname}${request.nextUrl.search}`,
  );
  return NextResponse.redirect(
    new URL(`/lock?next=${encodeURIComponent(returnPath)}`, `${proto}://${host}`),
    302,
  );
}

export const config = {
  matcher: [
    "/backend/api/v1/:path*",
    "/((?!api(?:/|$)|backend(?:/|$)|mcp(?:/|$)|_next(?:/|$)|favicon\\.ico$|icon\\.svg$).*)",
  ],
};
