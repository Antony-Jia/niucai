import { fireEvent, render, screen } from "@testing-library/react";
import {
  QueryClient,
  QueryClientProvider,
  useQuery,
} from "@tanstack/react-query";
import { expect, it, vi } from "vitest";
import { ScreensPanel } from "../src/components/ScreensPanel";
import { openNodePreview, request } from "../src/lib/api";

vi.mock("../src/lib/state", () => ({
  useApp: () => ({
    connected: true,
    demo: false,
    notify: vi.fn(),
    synchronizing: false,
    profile: { base_url: "http://127.0.0.1:8080", computer_url: "" },
  }),
  useData: (key: unknown[], fn: () => Promise<unknown>) =>
    useQuery({ queryKey: key, queryFn: fn }),
  useCommand: () => ({ mutate: vi.fn(), isPending: false }),
  queryClient: new QueryClient(),
}));
vi.mock("../src/lib/api", () => ({
  request: vi.fn(),
  native: () => true,
  setDemo: vi.fn(),
  openNodePreview: vi.fn(async () => undefined),
  openComputer: vi.fn(),
  changeComputerControl: vi.fn(),
  toggleComputerFullscreen: vi.fn(),
  api: {
    computers: async () => [
      {
        id: "c1",
        name: "Cloud Computer",
        kind: "linux",
        status: "ONLINE",
        control: "AGENT",
        state: {},
      },
    ],
  },
}));
vi.mock("../src/pages/Dashboard", () => ({ Approvals: () => null }));

const node = (
  id: string,
  name: string,
  preview_url: string,
  status = "ONLINE",
) => ({
  id,
  name,
  resource_type: "local_docker",
  platform: "linux",
  runner_version: "p1",
  status,
  enabled: true,
  slots: 1,
  busy: 0,
  engines: {},
  preview_url,
  paired_at: null,
  heartbeat_at: null,
  bots: [],
});

function mount() {
  vi.mocked(request).mockResolvedValue({
    kernel: { instance_id: "k", remote_enabled: true },
    nodes: [
      node("n1", "no-preview", ""),
      node("n2", "vnc-node", "https://vnc.example.com/vnc.html"),
    ],
  });
  render(
    <QueryClientProvider client={new QueryClient()}>
      <ScreensPanel />
    </QueryClientProvider>,
  );
}

it("lists computers and nodes in one switchable list", async () => {
  mount();
  const list = await screen.findByRole("list", { name: "远程画面列表" });
  expect(list.textContent).toContain("Cloud Computer");
  expect(list.textContent).toContain("可接管");
  expect(list.textContent).toContain("vnc-node");
  expect(list.textContent).toContain("仅查看");
  expect(list.textContent).toContain("无预览");
  // 有预览的节点排在没有预览的前面。
  expect(list.textContent!.indexOf("vnc-node")).toBeLessThan(
    list.textContent!.indexOf("no-preview"),
  );
});

it("opens the node preview through the registered address", async () => {
  mount();
  fireEvent.click(await screen.findByRole("button", { name: /vnc-node/ }));
  fireEvent.click(await screen.findByRole("button", { name: /打开远程画面/ }));
  expect(openNodePreview).toHaveBeenCalledWith("n2");
});

it("explains a node without preview and offers no open button", async () => {
  mount();
  fireEvent.click(await screen.findByRole("button", { name: /no-preview/ }));
  expect(await screen.findByText("该节点未提供预览")).toBeTruthy();
  expect(screen.queryByRole("button", { name: /打开远程画面/ })).toBeNull();
});
