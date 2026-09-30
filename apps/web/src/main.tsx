import "./bridge";
import React from "react";
import ReactDOM from "react-dom/client";
import { Provider } from "../../desktop/src/lib/state";
import { MobileApp } from "./App";
import "../../desktop/src/styles.css";
import "./mobile.css";
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <Provider>
      <MobileApp />
    </Provider>
  </React.StrictMode>,
);
if ("serviceWorker" in navigator && import.meta.env.PROD)
  void navigator.serviceWorker.register("/sw.js").catch(() => {});
