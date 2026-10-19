import { NextRequest, NextResponse } from "next/server";

/**
 * Basic Auth for the human-facing pages.
 *
 * Vercel's built-in password protection is a Pro-plan feature; this is the
 * equivalent for Hobby. One shared password for the stakeholder.
 *
 * Machine routes are exempt and carry their own bearer tokens instead -- a
 * headless worker polling every 30s should not sit behind an interactive
 * browser credential prompt.
 */
const MACHINE_ROUTES = [
  "/api/jobs", "/api/sync", "/api/worker", "/api/overrides/", "/api/category/import",
];

function isMachineRoute(pathname: string): boolean {
  // /api/overrides (the stakeholder's submit) is human; /api/overrides/<id>/complete is not.
  if (pathname.startsWith("/api/overrides/")) return pathname.endsWith("/complete");
  return MACHINE_ROUTES.some((p) => pathname.startsWith(p) && p !== "/api/overrides/");
}

function unauthorized(): NextResponse {
  return new NextResponse("Authentication required", {
    status: 401,
    headers: { "WWW-Authenticate": 'Basic realm="Catalogue review", charset="UTF-8"' },
  });
}

export function middleware(req: NextRequest) {
  const { pathname } = req.nextUrl;

  if (isMachineRoute(pathname)) return NextResponse.next();

  const expected = process.env.APP_PASSWORD;
  // Fail closed. An unset password must not silently publish the catalogue.
  if (!expected) {
    return new NextResponse(
      "APP_PASSWORD is not configured. Set it in the environment before serving.",
      { status: 500 },
    );
  }

  const header = req.headers.get("authorization");
  if (!header?.startsWith("Basic ")) return unauthorized();

  let decoded: string;
  try {
    decoded = atob(header.slice(6));
  } catch {
    return unauthorized();
  }
  // "user:password" -- the username is ignored, only the password is checked.
  const supplied = decoded.slice(decoded.indexOf(":") + 1);

  if (!timingSafeEqual(supplied, expected)) return unauthorized();

  return NextResponse.next();
}

/** Constant-time compare, so response latency can't be used to guess the password. */
function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export const config = {
  // Everything except Next's static assets and the favicon.
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
