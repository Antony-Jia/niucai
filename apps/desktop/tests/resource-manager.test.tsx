import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { QueryClient } from "@tanstack/react-query";
import {
  ResourceManager,
  type ResourceNode,
} from "../src/components/ResourceManager";
import { request } from "../src/lib/api";
import { downloadResourcePackage } from "../src/lib/resource-package";

vi.mock("../src/lib/state", () => ({
  useApp: () => ({ profile: null }),
  queryClient: new QueryClient(),
}));
vi.mock("../src/lib/api", () => ({
  request: vi.fn(),
  setDemo: vi.fn(),
  native: () => false,
}));
vi.mock("../src/lib/resource-package", async (original) => ({
  ...(await original<object>()),
  downloadResourcePackage: vi.fn(async () => true),
}));
afterEach(() => vi.clearAllMocks());
const node: ResourceNode = {
  id: "node",
  name: "Docker A",
  resource_type: "local_docker",
  status: "ONLINE",
  connection_status: "ONLINE",
  enabled: 1,
  heartbeat_at: null,
  capabilities: { available: true, version: "1.1.0" },
  active_job: null,
};
const act = async (action: () => Promise<unknown>) => {
  await action();
};

function fields(vps = false) {
  fireEvent.change(screen.getByLabelText("资源名称"), {
    target: { value: "New node" },
  });
  if (vps)
    fireEvent.change(screen.getByLabelText("资源类型"), {
      target: { value: "remote_vps" },
    });
  fireEvent.change(screen.getByLabelText("节点可访问的 Kernel 地址"), {
    target: {
      value: vps
        ? "https://kernel.example.com"
        : "http://host.docker.internal:8080",
    },
  });
  fireEvent.change(screen.getByLabelText("模型 model"), {
    target: { value: "model" },
  });
}

it.each([false, true])(
  "creates and downloads the selected resource type, then clears credentials (%s)",
  async (vps) => {
    vi.mocked(request).mockResolvedValue({
      id: "new-node",
      token: "one-time-token",
    });
    render(<ResourceManager nodes={[]} enabled busy={false} act={act} />);
    fireEvent.click(screen.getByRole("button", { name: "新增资源" }));
    fields(vps);
    fireEvent.click(screen.getByRole("button", { name: "创建资源并生成配置" }));
    fireEvent.click(await screen.findByRole("button", { name: "下载配置包" }));
    await waitFor(() =>
      expect(downloadResourcePackage).toHaveBeenCalledWith(
        expect.objectContaining({
          resourceType: vps ? "remote_vps" : "local_docker",
        }),
        { id: "new-node", token: "one-time-token" },
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: "关闭并清除凭据" }));
    fireEvent.click(screen.getByRole("button", { name: "新增资源" }));
    expect(
      screen.queryByRole("button", { name: "下载配置包" }),
    ).not.toBeInTheDocument();
  },
);

it("separates connectivity from scheduling and blocks management of LOST slots", () => {
  render(
    <ResourceManager
      nodes={[
        { ...node, enabled: 0 },
        {
          ...node,
          id: "lost",
          name: "Lost node",
          active_job: { id: "job", status: "LOST" },
        },
      ]}
      enabled
      busy={false}
      act={act}
    />,
  );
  expect(screen.getByRole("button", { name: "启用调度" })).toBeEnabled();
  expect(screen.getByRole("button", { name: "停用调度" })).toBeDisabled();
  expect(
    screen.getAllByRole("button", { name: "重置接入凭据" })[1],
  ).toBeDisabled();
  expect(screen.getByText(/原任务确认退出/)).toBeInTheDocument();
});

it("shows heartbeat arrival and clears one-time credentials on disconnect", async () => {
  vi.mocked(request).mockResolvedValue({
    id: "new-node",
    token: "one-time-token",
  });
  const view = render(
    <ResourceManager nodes={[]} enabled busy={false} act={act} />,
  );
  fireEvent.click(screen.getByRole("button", { name: "新增资源" }));
  fields();
  fireEvent.click(screen.getByRole("button", { name: "创建资源并生成配置" }));
  await screen.findByRole("button", { name: "下载配置包" });
  view.rerender(
    <ResourceManager
      nodes={[{ ...node, id: "new-node" }]}
      enabled
      busy={false}
      act={act}
    />,
  );
  expect(screen.getByText(/节点已上线/)).toBeInTheDocument();
  view.rerender(
    <ResourceManager nodes={[]} enabled={false} busy={false} act={act} />,
  );
  expect(
    screen.queryByRole("button", { name: "下载配置包" }),
  ).not.toBeInTheDocument();
});
