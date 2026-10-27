import React, { lazy, Suspense } from "react";
import ReactDOM from "react-dom/client";
const desktop = location.pathname.startsWith("/desktop");
const App = desktop ? lazy(() => import("./desktop/DesktopApp")) : lazy(() => import("./App"));
if (!desktop) void import("./styles.css");

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <Suspense fallback={<div style={{ padding: 30 }}>正在加载…</div>}><App /></Suspense>
  </React.StrictMode>,
);
