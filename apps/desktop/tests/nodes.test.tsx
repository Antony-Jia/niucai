import { fireEvent, render, screen } from "@testing-library/react";
import {
  QueryClient,
  QueryClientProvider,
  useQuery,
} from "@tanstack/react-query";
import { expect, it, vi } from "vitest";
import {
  NodesPanel,
  nodeKernelUrl,
  runCommand,
} from "../src/components/NodesPanel";
import { request } from "../src/lib/api";

vi.mock("../src/lib/state", () => ({
  useApp: () => ({
    connected: true,
    demo: false,
    notify: vi.fn(),
    profile: { base_url: "http://127.0.0.1:8080" },
  }),
  useData: (key: unknown[], fn: () => Promise<unknown>) =>
    useQuery({ queryKey: key, queryFn: fn }),
  queryClient: new QueryClient(),
}));
vi.mock("../src/lib/api", () => ({ request: vi.fn(), setDemo: vi.fn() }));

const node = {
  id: "n1",
  name: "VPS-1",
  resource_type: "remote_vps",
  platform: "linux",
  runner_version: "p1",
  status: "ONLINE",
  enabled: true,
  slots: 2,
  busy: 1,
  engines: { pi: { available: true } },
  preview_url: "https://vps-1.example.com/",
  paired_at: "2026-10-29T00:00:00",
  heartbeat_at: "2026-10-29T00:00:00",
  bots: [],
};

function mount() {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <NodesPanel />
    </QueryClientProvider>,
  );
}

it("draws the topology and lists nodes with slots", async () => {
  vi.mocked(request).mockResolvedValue({
    kernel: { instance_id: "k", remote_enabled: true },
    nodes: [node],
  } as never);
  mount();
  expect(await screen.findByLabelText("拓扑：Kernel 与 1 个节点")).toBeTruthy();
  expect(screen.getByText("1/2")).toBeTruthy();
  expect(screen.getByText("在线")).toBeTruthy();
  expect(screen.getByText("打开画面预览")).toBeTruthy();
});

it("shows no-bot hint and pairs a new node", async () => {
  vi.mocked(request).mockImplementation(async (path: string) =>
    path === "/api/pairing"
      ? ({ code: "ABCD-EFGH", expires_at: "", name: "VPS-2" } as never)
      : ({
          kernel: { instance_id: "k", remote_enabled: true },
          nodes: [],
        } as never),
  );
  mount();
  expect(await screen.findByText(/无可用 Bot/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "添加节点" }));
  fireEvent.change(screen.getByPlaceholderText("例如：VPS-东京"), {
    target: { value: "VPS-2" },
  });
  fireEvent.click(screen.getByText("生成配对码"));
  expect(await screen.findByText("ABCD-EFGH")).toBeTruthy();
  expect(request).toHaveBeenCalledWith("/api/pairing", "POST", {
    name: "VPS-2",
    resource_type: "remote_vps",
  });
  expect(screen.getByText("等待节点上线…")).toBeTruthy();
});

it("rewrites loopback Kernel URLs for local Docker nodes", () => {
  expect(nodeKernelUrl("http://127.0.0.1:8080", "local_docker")).toBe(
    "http://host.docker.internal:8080",
  );
  expect(nodeKernelUrl("http://127.0.0.1:8080", "remote_vps")).toBe(
    "http://127.0.0.1:8080",
  );
  expect(runCommand("https://k", "AAAA-BBBB")).toContain(
    "NIUCAI_PAIRING_CODE=AAAA-BBBB",
  );
  expect(runCommand("https://k", "AAAA-BBBB")).not.toMatch(
    /NIUCAI_PI_PROVIDER_KEY=/,
  );
});
