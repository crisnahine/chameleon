export type AuthToken = { userId: string; expiresAt: number };

export function createToken(userId: string, ttlMs = 3600_000): AuthToken {
  return { userId, expiresAt: Date.now() + ttlMs };
}

export function isValid(token: AuthToken): boolean {
  return Date.now() < token.expiresAt;
}
