type SpinnerProps = {
  size?: "small" | "large";
};

export function Spinner({ size = "small" }: SpinnerProps) {
  return <div className={`spinner spinner-${size}`} aria-label="loading" />;
}
