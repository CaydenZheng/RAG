import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { AccountBoundary } from "./app/AccountBoundary";
import { applyTheme, readTheme } from "./app/useTheme";
import "./styles/index.css";
import "./styles/skills.css";
import "./styles/resources.css";

const root = document.getElementById("root");
applyTheme(readTheme());
if (!root) throw new Error("Application root is missing");

createRoot(root).render(
  <StrictMode>
    <AccountBoundary />
  </StrictMode>,
);
