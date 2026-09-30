import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";
import { queryClient } from "../src/lib/state";
import { setDemo } from "../src/lib/api";
afterEach(() => {
  cleanup();
  queryClient.clear();
  setDemo(false);
});
Element.prototype.scrollIntoView = () => {};
