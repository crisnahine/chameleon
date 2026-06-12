export function truncateText(text: string, maxLength: number): string {
  if (maxLength <= 1) return text.slice(0, maxLength);
  if (text.length <= maxLength) return text;
  return text.slice(0, maxLength - 1) + "…";
}
