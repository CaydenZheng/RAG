import { MoonStar, Sun } from "lucide-react";

import type { ResolvedTheme, ThemePreference } from "../app/useTheme";

interface ThemeControlProps {
  preference: ThemePreference;
  resolved: ResolvedTheme;
  onChange: (preference: ThemePreference) => void;
}

export function ThemeControl({ preference, resolved, onChange }: ThemeControlProps): React.JSX.Element {
  return (
    <div className="theme-control">
      {resolved === "dark"
        ? <MoonStar size={17} aria-hidden="true" />
        : <Sun size={17} aria-hidden="true" />}
      <label className="sr-only" htmlFor="themePreference">外观主题</label>
      <select
        id="themePreference"
        value={preference}
        aria-label="外观主题"
        onChange={(event) => onChange(event.target.value as ThemePreference)}
      >
        <option value="system">跟随系统</option>
        <option value="light">浅色</option>
        <option value="dark">深色</option>
      </select>
    </div>
  );
}
