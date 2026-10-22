import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { it, expect, vi } from "vitest";
import { Provider } from "../src/lib/state";
import {
  ConnectionList,
  ConnectionSwitcher,
} from "../src/components/Connections";

const local = {
  kernel_instance_id: "local-1",
  name: "本机",
  base_url: "http://127.0.0.1:8080",
  computer_url: "",
  remember_token: true,
  has_token: true,
  platform: "windows",
};
const server = {
  ...local,
  kernel_instance_id: "server-1",
  name: "服务器",
  base_url: "https://agent.example.com",
};
const { invoke } = vi.hoisted(() => ({
  invoke: vi.fn(async (command: string, args?: { key?: string }) => {
    if (command === "load_profile") return local;
    if (command === "list_connections")
      return { active: "local-1", connections: [local, server] };
    if (command === "probe_connections")
      return [
        { key: "local-1", status: "online" },
        {
          key: "server-1",
          status: "auth",
          error: "API Token 无效或已过期，请重新连接",
        },
      ];
    if (command === "activate_connection")
      return args?.key === "server-1" ? server : local;
    if (command === "api_request") return [];
    return null;
  }),
}));
vi.mock("@tauri-apps/api/core", () => ({ isTauri: () => true, invoke }));
vi.mock("@tauri-apps/api/event", () => ({
  listen: vi.fn(async () => () => {}),
}));

it("lists saved connections with status and switches between them", async () => {
  render(
    <Provider>
      <ConnectionSwitcher />
      <ConnectionList onAdd={() => {}} />
    </Provider>,
  );
  expect(await screen.findByText("https://agent.example.com")).toBeTruthy();
  expect(await screen.findByText("Token 失效")).toBeTruthy();
  expect(screen.getByText(/当前 · 在线/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "切换" }));
  await waitFor(() =>
    expect(invoke).toHaveBeenCalledWith("activate_connection", {
      key: "server-1",
    }),
  );
  await waitFor(() =>
    expect(
      invoke.mock.calls.filter(([name]) => name === "start_events").length,
    ).toBeGreaterThan(0),
  );
});

it("removes a saved connection", async () => {
  render(
    <Provider>
      <ConnectionList onAdd={() => {}} />
    </Provider>,
  );
  fireEvent.click(
    await screen.findByRole("button", { name: "删除连接 服务器" }),
  );
  await waitFor(() =>
    expect(invoke).toHaveBeenCalledWith("remove_connection", {
      key: "server-1",
    }),
  );
});
