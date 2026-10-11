import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import { RemotePi } from "../src/pages/RemotePi";
import { request } from "../src/lib/api";

vi.mock("../src/lib/state", () => ({
  useApp: () => ({ connected: true, demo: false, notify: vi.fn() }),
  queryClient: new QueryClient(),
}));
vi.mock("../src/lib/api", () => ({ request: vi.fn(), setDemo: vi.fn() }));
afterEach(() => vi.clearAllMocks());

function mount(status: string) {
  vi.mocked(request).mockImplementation(async (path) => {
    if (path === "/api/remote/nodes") return [] as never;
    if (path === "/api/remote/jobs")
      return [
        { id: "child", prompt: "Fix login", status, node_id: null, result: {} },
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
    screen.getByText("批准此工作包，允许远程 Pi 在隔离节点内自主执行。"),
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
