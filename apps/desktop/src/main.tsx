import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import { Provider } from "./lib/state";
import "./styles.css";
import "./light-theme.css";
import "./polish.css";
import "./simplified.css";
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <Provider>
      <App />
    </Provider>
  </React.StrictMode>,
);
