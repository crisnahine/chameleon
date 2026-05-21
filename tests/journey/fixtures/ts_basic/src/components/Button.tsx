import { type ReactNode } from "react";

type ButtonProps = {
  children: ReactNode;
  onClick: () => void;
  variant?: "primary" | "secondary";
};

export function Button({ children, onClick, variant = "primary" }: ButtonProps) {
  return (
    <button className={`btn btn-${variant}`} onClick={onClick}>
      {children}
    </button>
  );
}
