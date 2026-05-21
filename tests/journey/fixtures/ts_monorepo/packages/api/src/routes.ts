type Handler = (req: unknown, res: unknown) => void;

export const routes: { path: string; method: string; handler: Handler }[] = [
  { path: "/health", method: "GET", handler: (_req, res: any) => res.json({ ok: true }) },
  { path: "/users", method: "GET", handler: (_req, res: any) => res.json([]) },
];
