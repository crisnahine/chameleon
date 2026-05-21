import { useState, type ReactNode } from "react";

type Tab = { id: string; label: string; content: ReactNode };

type TabsProps = { tabs: Tab[] };

export function Tabs({ tabs }: TabsProps) {
  const [active, setActive] = useState(tabs[0]?.id ?? "");
  return (
    <div className="tabs">
      <div className="tab-bar">
        {tabs.map((t) => (
          <button
            key={t.id}
            className={`tab-btn ${active === t.id ? "active" : ""}`}
            onClick={() => setActive(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div className="tab-panel">
        {tabs.find((t) => t.id === active)?.content}
      </div>
    </div>
  );
}
