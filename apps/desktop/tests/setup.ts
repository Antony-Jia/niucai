import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";
afterEach(async () => {
  cleanup();
  const { queryClient } = await import("../src/lib/state");
  const { setDemo } = await import("../src/lib/api");
  queryClient.clear();
  setDemo(false);
});
Element.prototype.scrollIntoView = () => {};
