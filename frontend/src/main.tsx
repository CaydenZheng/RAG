import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { AccountBoundary } from "./app/AccountBoundary";
import "./styles/index.css";
import "./styles/skills.css";

const root = document.getElementById("root");
if (!root) throw new Error("Application root is missing");

createRoot(root).render(
  <StrictMode>
    <AccountBoundary />
  </StrictMode>,
);
