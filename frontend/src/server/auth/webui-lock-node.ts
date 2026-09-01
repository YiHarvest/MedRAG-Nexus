import { createHash, timingSafeEqual } from "node:crypto";

export function webUiPasswordMatches(
  candidate: string | null | undefined,
  expected: string,
) {
  if (!candidate) return false;
  const candidateHash = createHash("sha256").update(candidate).digest();
  const expectedHash = createHash("sha256").update(expected).digest();
  return timingSafeEqual(candidateHash, expectedHash);
}
