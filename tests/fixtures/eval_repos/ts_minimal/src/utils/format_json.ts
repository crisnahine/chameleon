export const formatJson = (obj: object): string => {
  return JSON.stringify(obj, null, 2);
};
