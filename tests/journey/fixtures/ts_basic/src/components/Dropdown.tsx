import { useState } from "react";

type DropdownProps = {
  options: { value: string; label: string }[];
  onChange: (value: string) => void;
  placeholder?: string;
};

export function Dropdown({ options, onChange, placeholder = "Select..." }: DropdownProps) {
  const [open, setOpen] = useState(false);
  return (
    <div className="dropdown">
      <button onClick={() => setOpen((o) => !o)}>{placeholder}</button>
      {open && (
        <ul className="dropdown-menu">
          {options.map((opt) => (
            <li key={opt.value} onClick={() => { onChange(opt.value); setOpen(false); }}>
              {opt.label}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
