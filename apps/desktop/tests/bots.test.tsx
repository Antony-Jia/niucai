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

it("offers the models the node reported and resets the choice when the engine changes", async () => {
  const { BotForm } = await import("../src/components/BotForm");
  render(
    <QueryClientProvider client={new QueryClient()}>
      <BotForm
        nodeId="n1"
        engines={["pi", "claude"]}
        models={{ pi: ["pi-a", "pi-b"], claude: ["sonnet"] }}
        onClose={() => {}}
      />
    </QueryClientProvider>,
  );
  const model = () => screen.getByLabelText("模型") as HTMLSelectElement;
  const options = () => [...model().options].map((o) => o.value);
  expect(options()).toEqual(["", "pi-a", "pi-b"]);
  fireEvent.change(model(), { target: { value: "pi-b" } });
  expect(model().value).toBe("pi-b");
  fireEvent.change(screen.getByLabelText("引擎"), {
    target: { value: "claude" },
  });
  expect(options()).toEqual(["", "sonnet"]);
  expect(model().value).toBe("");
});

it("keeps a saved model the node no longer reports and falls back to a text box", async () => {
  const { BotForm } = await import("../src/components/BotForm");
  const bot = {
    id: "b1",
    name: "coder",
    node_id: "n1",
    engine: "pi",
    role: "",
    instructions: "",
    tags: [],
    model: "old-model",
    permission_profile: "workspace",
    skills: [],
    mcp_servers: [],
    requires_approval: false,
    concurrency: 1,
    enabled: true,
    running: 0,
    config_version: 1,
  } as never;
  const view = render(
    <QueryClientProvider client={new QueryClient()}>
      <BotForm
        nodeId="n1"
        engines={["pi"]}
        models={{ pi: ["pi-a"] }}
        bot={bot}
        onClose={() => {}}
      />
    </QueryClientProvider>,
  );
  const model = screen.getByLabelText("模型") as HTMLSelectElement;
  expect(model.value).toBe("old-model");
  expect([...model.options].map((o) => o.textContent)).toContain(
    "old-model（节点未上报）",
  );
  view.unmount();
  render(
    <QueryClientProvider client={new QueryClient()}>
      <BotForm nodeId="n1" engines={["pi"]} bot={bot} onClose={() => {}} />
    </QueryClientProvider>,
  );
  expect(screen.getByLabelText("模型").tagName).toBe("INPUT");
});

it("offers reasoning effort only when the node reports it and resets it per engine", async () => {
  const { BotForm } = await import("../src/components/BotForm");
  const view = render(
    <QueryClientProvider client={new QueryClient()}>
      <BotForm
        nodeId="n1"
        engines={["pi", "claude"]}
        efforts={{ pi: ["off", "high"], claude: ["low", "high"] }}
        onClose={() => {}}
      />
    </QueryClientProvider>,
  );
  const effort = () => screen.getByLabelText("推理强度") as HTMLSelectElement;
  expect([...effort().options].map((o) => o.value)).toEqual([
    "",
    "off",
    "high",
  ]);
  fireEvent.change(effort(), { target: { value: "off" } });
  fireEvent.change(screen.getByLabelText("引擎"), {
    target: { value: "claude" },
  });
  expect([...effort().options].map((o) => o.value)).toEqual([
    "",
    "low",
    "high",
  ]);
  expect(effort().value).toBe("");
  view.unmount();
  render(
    <QueryClientProvider client={new QueryClient()}>
      <BotForm nodeId="n1" engines={["pi"]} onClose={() => {}} />
    </QueryClientProvider>,
  );
  expect(screen.queryByLabelText("推理强度")).toBeNull();
});
