export type DbConfig = { host: string; port: number; name: string };

export function createPool(config: DbConfig) {
  return { config, query: async (sql: string) => ({ rows: [], sql }) };
}
