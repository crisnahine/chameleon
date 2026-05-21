export type ApiResponse<T> = {
  data: T;
  meta: { total: number; page: number };
};
