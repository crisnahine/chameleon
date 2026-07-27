import { cx } from "../utils/cx";

type SpinnerProps = {
  size?: "small" | "large";
  inline?: boolean;
};

export function Spinner({ size = "small", inline = false }: SpinnerProps) {
  return (
    <div
      className={cx("spinner", `spinner-${size}`, inline && "spinner-inline")}
      aria-label="loading"
    />
  );
}
