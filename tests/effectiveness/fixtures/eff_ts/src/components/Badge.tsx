type BadgeProps = {
  label: string;
  tone?: "green" | "red" | "gray";
};

export function Badge({ label, tone = "gray" }: BadgeProps) {
  return <span className={`badge badge-${tone}`}>{label}</span>;
}
