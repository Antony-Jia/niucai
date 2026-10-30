import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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
vi.mock("../src/lib/api", () => ({
  request: vi.fn(),
  setDemo: vi.fn(),
  native: () => false,
}));

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
  vi.mocked(request).mockImplementation(
    async (path: string) =>
      (path === "/api/bots"
        ? []
        : {
            kernel: { instance_id: "k", remote_enabled: true },
            nodes: [node],
          }) as never,
  );
  mount();
  expect(await screen.findByLabelText("拓扑：Kernel 与 1 个节点")).toBeTruthy();
  expect(screen.getByText("1/2")).toBeTruthy();
  expect(screen.getByText("在线")).toBeTruthy();
  expect(screen.getByText("查看远程画面")).toBeTruthy();
});

it("shows no-bot hint and pairs a new node", async () => {
  vi.mocked(request).mockImplementation(async (path: string) =>
    path === "/api/pairing"
      ? ({ code: "ABCD-EFGH", expires_at: "", name: "VPS-2" } as never)
      : path === "/api/bots"
        ? ([] as never)
        : ({
            kernel: { instance_id: "k", remote_enabled: true },
            nodes: [],
          } as never),
  );
  mount();
  expect(await screen.findByText(/无可用bot/)).toBeTruthy();
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

it("lists bots under a node and adds one", async () => {
  const bot = {
    id: "b1",
    name: "coder",
    engine: "claude",
    role: "开发",
    enabled: true,
    running: 1,
    concurrency: 2,
  };
  vi.mocked(request).mockImplementation(async (path: string) => {
    if (path === "/api/bots") return [] as never;
    if (path === "/api/topology")
      return {
        kernel: { instance_id: "k", remote_enabled: true },
        nodes: [
          {
            ...node,
            engines: { pi: { available: true }, claude: { available: true } },
            bots: [bot],
          },
        ],
      } as never;
    return {} as never;
  });
  mount();
  expect(await screen.findByText("@coder · claude · 开发 · 1/2")).toBeTruthy();
  expect(screen.getByText("@coder · claude · 执行中 1")).toBeTruthy();
  fireEvent.click(screen.getByText("添加 Bot"));
  fireEvent.change(screen.getByPlaceholderText("例如：coder"), {
    target: { value: "writer" },
  });
  fireEvent.change(screen.getByLabelText("引擎"), {
    target: { value: "claude" },
  });
  fireEvent.change(screen.getByPlaceholderText("frontend, docs"), {
    target: { value: "docs, 写作" },
  });
  fireEvent.click(screen.getByText("保存"));
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith(
      "/api/bots",
      "POST",
      expect.objectContaining({
        name: "writer",
        node_id: "n1",
        engine: "claude",
        tags: ["docs", "写作"],
        concurrency: 1,
        requires_approval: false,
      }),
    ),
  );
});

it("defaults new bots on Windows nodes to read-only with approval", async () => {
  vi.mocked(request).mockImplementation(async (path: string) => {
    if (path === "/api/bots") return [] as never;
    if (path === "/api/topology")
      return {
        kernel: { instance_id: "k", remote_enabled: true },
        nodes: [
          {
            ...node,
            platform: "windows",
            resource_type: "windows_local",
            engines: { claude: { available: true } },
          },
        ],
      } as never;
    return {} as never;
  });
  mount();
  expect(await screen.findByText(/本机 Windows · windows/)).toBeTruthy();
  fireEvent.click(screen.getByText("添加 Bot"));
  fireEvent.change(screen.getByPlaceholderText("例如：coder"), {
    target: { value: "local" },
  });
  fireEvent.click(screen.getByText("保存"));
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith(
      "/api/bots",
      "POST",
      expect.objectContaining({
        name: "local",
        permission_profile: "read",
        requires_approval: true,
      }),
    ),
  );
});

it("warns about Pi MCP and missing bot assets", async () => {
  const { BotForm } = await import("../src/components/BotForm");
  render(
    <QueryClientProvider client={new QueryClient()}>
      <BotForm
        nodeId="n1"
        engines={["pi"]}
        bot={{
          id: "b1",
          name: "coder",
          node_id: "n1",
          engine: "pi",
          role: "",
          instructions: "",
          tags: [],
          model: "",
          permission_profile: "workspace",
          skills: ["ghost"],
          mcp_servers: ["fs"],
          requires_approval: false,
          concurrency: 1,
          enabled: true,
          running: 0,
          config_version: 1,
          missing_assets: ["skill:ghost"],
          node_env: ["FS_TOKEN"],
        }}
        onClose={() => {}}
      />
    </QueryClientProvider>,
  );
  expect(screen.getByText("Pi Bot 暂不支持 MCP")).toBeTruthy();
  expect(screen.getByText(/FS_TOKEN/)).toBeTruthy();
  expect(screen.getByText(/skill:ghost/)).toBeTruthy();
});
