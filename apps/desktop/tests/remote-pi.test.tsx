import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import { RemotePi } from "../src/pages/RemotePi";
import { request } from "../src/lib/api";

const notify = vi.hoisted(() => vi.fn());

vi.mock("../src/lib/state", () => ({
  useApp: () => ({ connected: true, demo: false, notify }),
  queryClient: new QueryClient(),
}));
vi.mock("../src/lib/api", () => ({ request: vi.fn(), setDemo: vi.fn() }));
afterEach(() => vi.clearAllMocks());

function mount(status: string, extra: object = {}) {
  vi.mocked(request).mockImplementation(async (path) => {
    if (path === "/api/remote/config") return { enabled: true } as never;
    if (path === "/api/remote/nodes") return [] as never;
    if (path === "/api/remote/jobs")
      return [
        {
          id: "child",
          prompt: "Fix login",
          status,
          node_id: null,
          result: {},
          ...extra,
        },
      ] as never;
    return {} as never;
  });
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <RemotePi />
    </QueryClientProvider>,
  );
}

it("requires an explicit workspace-autonomy approval before remote execution", async () => {
  mount("WAITING_APPROVAL");
  fireEvent.click(await screen.findByRole("button", { name: "Fix login" }));
  expect(
    screen.getByText(
      "批准此工作包，允许 Pi · 工作区读写 在隔离节点内自主执行。",
    ),
  ).toBeInTheDocument();
  expect(request).not.toHaveBeenCalledWith(
    "/api/remote/jobs/child/approve",
    "POST",
  );
  fireEvent.click(screen.getByRole("button", { name: "批准执行" }));
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith(
      "/api/remote/jobs/child/approve",
      "POST",
    ),
  );
});

it("does not offer retry while a lost process has not been reconciled", async () => {
  mount("LOST");
  fireEvent.click(await screen.findByRole("button", { name: "Fix login" }));
  expect(
    screen.getByText(
      "等待节点确认进程退出，资源尚未释放。此时不能启动重复执行。",
    ),
  ).toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "重试并重新授权" }),
  ).not.toBeInTheDocument();
});

it("reports a management conflict if a task claims the node after the idle snapshot", async () => {
  vi.mocked(request).mockImplementation(async (path, method) => {
    if (path === "/api/remote/config") return { enabled: true } as never;
    if (path === "/api/remote/jobs") return [] as never;
    if (path === "/api/remote/nodes")
      return [
        {
          id: "node",
          name: "Docker",
          resource_type: "local_docker",
          status: "ONLINE",
          connection_status: "ONLINE",
          enabled: 1,
          capabilities: {},
          active_job: null,
        },
      ] as never;
    if (method === "PATCH")
      throw new Error("节点仍有执行中、取消中或 LOST 任务，请先确认原任务退出");
    return {} as never;
  });
  render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <RemotePi />
    </QueryClientProvider>,
  );
  const button = await screen.findByRole("button", { name: "停用调度" });
  await waitFor(() => expect(button).toBeEnabled());
  fireEvent.click(button);
  await waitFor(() =>
    expect(notify).toHaveBeenCalledWith(expect.stringContaining("原任务退出")),
  );
});

it("shows the engine and permission profile on the approval card", async () => {
  mount("WAITING_APPROVAL", {
    engine: "claude",
    config_snapshot: { permission_profile: "full" },
  });
  fireEvent.click(await screen.findByRole("button", { name: "Fix login" }));
  expect(
    screen.getByText(
      "批准此工作包，允许 Claude Code · 完全访问 在隔离节点内自主执行。",
    ),
  ).toBeInTheDocument();
});

it("submits the chosen engine and permission profile", async () => {
  mount("COMPLETED");
  fireEvent.change(await screen.findByLabelText("执行引擎"), {
    target: { value: "codex" },
  });
  fireEvent.change(screen.getByLabelText("权限档位"), {
    target: { value: "read" },
  });
  fireEvent.change(screen.getByLabelText("工作要求"), {
    target: { value: "Review the repo" },
  });
  fireEvent.click(screen.getByRole("button", { name: "提交并查看授权" }));
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith(
      "/api/remote/jobs",
      "POST",
      expect.objectContaining({ engine: "codex", permission_profile: "read" }),
    ),
  );
});

it("continues a finished Claude session instead of starting a new one", async () => {
  mount("COMPLETED", { engine: "claude", session_id: "sess-1", node_id: "n1" });
  fireEvent.click(await screen.findByRole("button", { name: "Fix login" }));
  fireEvent.click(screen.getByRole("button", { name: "继续此会话" }));
  fireEvent.change(screen.getByLabelText("工作要求"), {
    target: { value: "Now add tests" },
  });
  fireEvent.click(screen.getByRole("button", { name: "提交并查看授权" }));
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith(
      "/api/remote/jobs",
      "POST",
      expect.objectContaining({ resume_job_id: "child" }),
    ),
  );
  const body = vi
    .mocked(request)
    .mock.calls.find((call) => call[1] === "POST")?.[2] as object;
  expect(body).not.toHaveProperty("engine");
  expect(body).not.toHaveProperty("allowed_nodes");
});

it("does not offer to continue a Pi job", async () => {
  mount("COMPLETED", { engine: "pi", session_id: "s", node_id: "n1" });
  fireEvent.click(await screen.findByRole("button", { name: "Fix login" }));
  expect(
    screen.queryByRole("button", { name: "继续此会话" }),
  ).not.toBeInTheDocument();
});
