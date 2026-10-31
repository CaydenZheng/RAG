import { useEffect, useState } from "react";

export type ThemePreference = "system" | "light" | "dark";
const KEY = "ragflow_theme";
export function readTheme(): ThemePreference {
  try { const value=localStorage.getItem(KEY);return value==="light"||value==="dark"?value:"system"; } catch { return "system"; }
}
export function applyTheme(value: ThemePreference): void { document.documentElement.dataset.theme=value; }
export function useTheme(): {theme: ThemePreference; changeTheme: (value: ThemePreference) => void} {
  const [theme,setTheme]=useState<ThemePreference>(readTheme);
  useEffect(()=>{applyTheme(theme);},[theme]);
  function changeTheme(value: ThemePreference): void {setTheme(value);try{localStorage.setItem(KEY,value);}catch{/* The current-page preference still applies. */}}
  return {theme,changeTheme};
}
