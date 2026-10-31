import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "@fontsource/host-grotesk/400.css";
import "@fontsource/host-grotesk/500.css";
import "@fontsource/atkinson-hyperlegible-mono/400.css";
import "@fontsource/atkinson-hyperlegible-mono/500.css";
import "./app.css";
import App from "./App";

createRoot(document.getElementById("root")).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
