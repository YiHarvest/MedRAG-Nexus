import {
  webUiRedirectResponse,
  WEBUI_LOCK_COOKIE_NAME,
} from "@/server/auth/webui-lock";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export function POST(request: Request) {
  const response = webUiRedirectResponse("/lock");
  response.cookies.set(WEBUI_LOCK_COOKIE_NAME, "", {
    httpOnly: true,
    secure: new URL(request.url).protocol === "https:",
    sameSite: "lax",
    maxAge: 0,
    path: "/",
  });
  response.headers.set("cache-control", "no-store");
  return response;
}
