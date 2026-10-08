import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { describe, expect, it, vi, afterEach } from "vitest";
import * as transport from "../src/lib/api";
import { Provider, useApp, useData } from "../src/lib/state";

const listeners = vi.hoisted(
  () => new Map<string, (event: { payload: unknown }) => void>(),
);
vi.mock("@tauri-apps/api/core", () => ({
  isTauri: () => true,
  invoke: vi.fn().mockResolvedValue(undefined),
}));
vi.mock("@tauri-apps/api/event", () => ({
  listen: vi.fn(
    async (name: string, callback: (event: { payload: unknown }) => void) => {
      listeners.set(name, callback);
      return () => listeners.delete(name);
    },
  ),
}));
afterEach(() => {
  vi.restoreAllMocks();
  listeners.clear();
});

function Harness() {
  const { connected, connectTo, synchronizing } = useApp();
  const tasks = useData(["tasks"], transport.api.tasks);
  const approvals = useData(["approvals"], transport.api.approvals);
  const computers = useData(["computers"], transport.api.computers);
  return (
    <>
      {!connected && (
        <button
          onClick={() =>
            void connectTo({
              base_url: "http://127.0.0.1:8080",
              token: "test",
            } as never)
          }
        >
          连接
        </button>
      )}
      <button disabled={synchronizing}>任务控制</button>
      <p>{tasks.data?.[0]?.status}</p>
      <p>审批：{approvals.data?.length}</p>
      <p>{computers.data?.[0]?.control}</p>
    </>
  );
}

describe("reconnection reconciliation", () => {
  it("refreshes task, approval and control snapshots without a new business event", async () => {
    const profile = { base_url: "http://127.0.0.1:8080", has_token: false };
    vi.spyOn(transport, "loadProfile").mockResolvedValue(profile as never);
    vi.spyOn(transport, "connect").mockResolvedValue({
      ...profile,
      has_token: true,
    } as never);
    vi.spyOn(transport.api, "events").mockResolvedValue([]);
    const tasks = vi
      .spyOn(transport.api, "tasks")
      .mockResolvedValue([{ status: "RUNNING" }] as never);
    const approvals = vi
      .spyOn(transport.api, "approvals")
      .mockResolvedValue([]);
    const computers = vi
      .spyOn(transport.api, "computers")
      .mockResolvedValue([{ control: "AGENT" }] as never);
    render(
      <Provider>
        <Harness />
      </Provider>,
    );
    fireEvent.click(screen.getByRole("button", { name: "连接" }));
    await waitFor(() => expect(listeners.has("kernel-stream")).toBe(true));
    await act(async () =>
      listeners.get("kernel-stream")?.({ payload: "WebSocket 同步" }),
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "任务控制" })).toBeEnabled(),
    );
    await act(async () =>
      listeners.get("kernel-stream")?.({ payload: "等待重连" }),
    );
    expect(screen.getByRole("button", { name: "任务控制" })).toBeDisabled();
    let finish!: (value: never) => void;
    tasks.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    approvals.mockResolvedValue([{ id: "approval" }] as never);
    computers.mockResolvedValue([{ control: "HUMAN" }] as never);
    await act(async () =>
      listeners.get("kernel-stream")?.({ payload: "轮询同步" }),
    );
    expect(screen.getByRole("button", { name: "任务控制" })).toBeDisabled();
    await act(async () => finish([{ status: "WAITING_HUMAN" }] as never));
    await screen.findByText("WAITING_HUMAN");
    expect(screen.getByText("审批：1")).toBeInTheDocument();
    expect(screen.getByText("HUMAN")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "任务控制" })).toBeEnabled(),
    );
  });
});
