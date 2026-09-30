import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { it, expect, vi } from "vitest";
import { Provider, useApp } from "../src/lib/state";
const { invoke } = vi.hoisted(() => ({
  invoke: vi.fn(async (command: string) => {
    if (command === "load_profile")
      return {
        base_url: "",
        computer_url: "",
        remember_token: false,
        has_token: false,
        platform: "windows",
      };
    if (command === "connect_kernel")
      return {
        base_url: "https://example.com",
        computer_url: "",
        remember_token: false,
        has_token: true,
        platform: "windows",
      };
    if (command === "api_request") return [];
    return null;
  }),
}));
vi.mock("@tauri-apps/api/core", () => ({ isTauri: () => true, invoke }));
vi.mock("@tauri-apps/api/event", () => ({
  listen: vi.fn(async () => () => {}),
}));
function Connection() {
  const { connectTo, stream } = useApp();
  return (
    <>
      <span>{stream}</span>
      <button
        onClick={() =>
          void connectTo({
            baseUrl: "https://example.com",
            computerUrl: "",
            token: "test-only-connection-token-000000000000",
            rememberToken: false,
          })
        }
      >
        连接
      </button>
    </>
  );
}
it("restarts event synchronization when an existing connection is reconfigured", async () => {
  render(
    <Provider>
      <Connection />
    </Provider>,
  );
  await waitFor(() => expect(invoke).toHaveBeenCalledWith("load_profile"));
  fireEvent.click(screen.getByRole("button", { name: "连接" }));
  await waitFor(() =>
    expect(
      invoke.mock.calls.filter(([name]) => name === "start_events"),
    ).toHaveLength(1),
  );
  fireEvent.click(screen.getByRole("button", { name: "连接" }));
  await waitFor(() =>
    expect(
      invoke.mock.calls.filter(([name]) => name === "start_events"),
    ).toHaveLength(2),
  );
});
