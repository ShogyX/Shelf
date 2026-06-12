import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "./App";
import "./index.css";

const queryClient = new QueryClient({
  // A small default staleTime dampens refetch storms from the many mutations that
  // invalidate shared keys (["works"], etc.) as the user navigates between pages.
  defaultOptions: { queries: { refetchOnWindowFocus: false, retry: 1, staleTime: 2000 } },
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>
);

// PWA service worker — only in a production build served over a secure context (SWs require
// https or localhost). The dev server (Vite proxy) skips it so HMR isn't shadowed by a cache.
if (import.meta.env.PROD && "serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {
      /* registration is best-effort — the app works fine without offline support */
    });
  });
}
