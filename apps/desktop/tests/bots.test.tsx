import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, expect, it, vi } from "vitest";
import { Bots } from "../src/pages/Bots";
import { request } from "../src/lib/api";

const state = vi.hoisted(() => ({
  connected: true,
  demo: false,
  synchronizing: false,
  notify: vi.fn(),
  setPage: vi.fn(),
}));
vi.mock("../src/lib/state", () => ({
  useApp: () => state,
  queryClient: new QueryClient(),
}));
vi.mock("../src/lib/api", () => ({ request: vi.fn(), setDemo: vi.fn() }));
const online = {
  id: "n1",
  name: "Docker Pi",
  status: "ONLINE",
  enabled: true,
  slots: 1,
  busy: 0,
  platform: "linux",
  engines: { pi: { available: true }, codex: { available: false } },
  bots: [],
};
beforeEach(() => {
  state.synchronizing = false;
  vi.mocked(request)
    .mockReset()
    .mockImplementation(async (path) =>
      path === "/api/topology"
        ? ({
            kernel: { remote_enabled: true },
            nodes: [
              online,
              { ...online, id: "n2", name: "VPS offline", status: "OFFLINE" },
            ],
          } as never)
        : ([] as never),
    );
});
function mount() {
  return render(
    <QueryClientProvider
      client={
        new QueryClient({ defaultOptions: { queries: { retry: false } } })
      }
    >
      <Bots />
    </QueryClientProvider>,
  );
}
it("creates a node Bot using a template and only available engines", async () => {
  mount();
  await screen.findByText("创建你的第一位 Bot");
  fireEvent.click(screen.getAllByRole("button", { name: "新建 Bot" })[0]);
  expect(screen.getByRole("button", { name: /VPS offline/ })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: /Docker Pi/ }));
  fireEvent.click(screen.getByRole("button", { name: "资料研究" }));
  fireEvent.change(screen.getByLabelText("名称（用于 @ 提及）"), {
    target: { value: "researcher" },
  });
  expect(screen.getByLabelText("引擎").querySelectorAll("option")).toHaveLength(
    1,
  );
  fireEvent.click(screen.getByRole("button", { name: "保存" }));
  await waitFor(() =>
    expect(request).toHaveBeenCalledWith(
      "/api/bots",
      "POST",
      expect.objectContaining({
        name: "researcher",
        node_id: "n1",
        engine: "pi",
        role: "研究与资料整理",
        tags: ["research", "docs"],
      }),
    ),
  );
  expect(
    vi.mocked(request).mock.calls.some(([path]) => path === "/api/agents"),
  ).toBe(false);
});
it("keeps an in-progress form and prevents saving after disconnect", async () => {
  const view = mount();
  await screen.findByText("创建你的第一位 Bot");
  fireEvent.click(screen.getAllByRole("button", { name: "新建 Bot" })[0]);
  fireEvent.click(screen.getByRole("button", { name: /Docker Pi/ }));
  fireEvent.change(screen.getByLabelText("名称（用于 @ 提及）"), {
    target: { value: "draft" },
  });
  state.synchronizing = true;
  view.rerender(
    <QueryClientProvider client={new QueryClient()}>
      <Bots />
    </QueryClientProvider>,
  );
  expect(screen.getByLabelText("名称（用于 @ 提及）")).toHaveValue("draft");
  expect(screen.getByRole("button", { name: "保存" })).toBeDisabled();
});
