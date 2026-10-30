import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, expect, it, vi } from "vitest";
import { LocalNode } from "../src/components/LocalNode";
import { useLocalRunnerAutostart } from "../src/lib/local-runner";
import { localRunner, request } from "../src/lib/api";

vi.mock("../src/lib/state", () => ({
  useApp: () => ({ notify: vi.fn() }),
  queryClient: new QueryClient(),
}));
vi.mock("../src/lib/api", () => ({
  request: vi.fn(),
  setDemo: vi.fn(),
  native: () => true,
  localRunner: {
    status: vi.fn(),
    start: vi.fn(),
    stop: vi.fn(),
    forget: vi.fn(),
  },
}));

const base = {
  running: false,
  wanted: false,
  paired: false,
  available: true,
  dataDir: "C:\\Users\\me\\AppData\\Local\\niucai-runner",
  lastError: "",
  log: "",
};

function mount() {
  render(
    <QueryClientProvider client={new QueryClient()}>
      <LocalNode onChange={() => {}} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  vi.spyOn(window, "confirm").mockReturnValue(true);
});

it("pairs this machine after the security warning and starts the runner", async () => {
  vi.mocked(localRunner.status).mockResolvedValue(base);
  vi.mocked(request).mockResolvedValue({ code: "ABCD-EFGH" } as never);
  mount();
  fireEvent.change(await screen.findByPlaceholderText("D:\\Projects\\demo"), {
    target: { value: "D:\\work\n\n E:\\repo " },
  });
  fireEvent.click(screen.getByText("把本机加为节点"));
  await waitFor(() =>
    expect(localRunner.start).toHaveBeenCalledWith("ABCD-EFGH", [
      "D:\\work",
      "E:\\repo",
    ]),
  );
  expect(window.confirm).toHaveBeenCalledWith(
    expect.stringContaining("Shell 不是安全沙箱"),
  );
  expect(request).toHaveBeenCalledWith("/api/pairing", "POST", {
    name: "本机 Windows",
    resource_type: "windows_local",
  });
  expect(localStorage.getItem("niucai.localRunner.autostart")).toBe("1");
});

it("does nothing when the warning is declined", async () => {
  vi.mocked(localRunner.status).mockResolvedValue(base);
  vi.mocked(window.confirm).mockReturnValue(false);
  mount();
  fireEvent.click(await screen.findByText("把本机加为节点"));
  await waitFor(() => expect(window.confirm).toHaveBeenCalled());
  expect(request).not.toHaveBeenCalled();
  expect(localRunner.start).not.toHaveBeenCalled();
});

it("drains a running node and shows errors and logs", async () => {
  localStorage.setItem("niucai.localRunner.autostart", "1");
  vi.mocked(localRunner.status).mockResolvedValue({
    ...base,
    paired: true,
    running: true,
    wanted: true,
    lastError: "Runner 意外退出（exit code: 1），2 秒后重启",
    log: "PAIRED: node n1",
  });
  mount();
  expect(await screen.findByText("运行中")).toBeTruthy();
  expect(screen.getByRole("alert").textContent).toContain("意外退出");
  expect(screen.getByText("PAIRED: node n1")).toBeTruthy();
  expect(localRunner.start).not.toHaveBeenCalled(); // 已在运行，不重复自启
  fireEvent.click(screen.getByText("停止（完成当前作业后）"));
  await waitFor(() => expect(localRunner.stop).toHaveBeenCalledWith("drain"));
  expect(localStorage.getItem("niucai.localRunner.autostart")).toBeNull();
});

function Autostart({ enabled }: { enabled: boolean }) {
  useLocalRunnerAutostart(enabled, () => {});
  return null;
}

it("autostarts a paired node that was running last time, without the devices page", async () => {
  localStorage.setItem("niucai.localRunner.autostart", "1");
  localStorage.setItem("niucai.localRunner.workdirAllow", "D:\work");
  vi.mocked(localRunner.status).mockResolvedValue({ ...base, paired: true });
  render(<Autostart enabled />);
  await waitFor(() =>
    expect(localRunner.start).toHaveBeenCalledWith(null, ["D:\work"]),
  );
});

it("does not autostart before connecting, after a manual stop, or when already running", async () => {
  vi.mocked(localRunner.status).mockResolvedValue({ ...base, paired: true });
  const { rerender } = render(<Autostart enabled={false} />);
  localStorage.setItem("niucai.localRunner.autostart", "1");
  rerender(<Autostart enabled={false} />);
  localStorage.removeItem("niucai.localRunner.autostart");
  rerender(<Autostart enabled />);
  vi.mocked(localRunner.status).mockResolvedValue({
    ...base,
    paired: true,
    running: true,
  });
  localStorage.setItem("niucai.localRunner.autostart", "1");
  render(<Autostart enabled />);
  await waitFor(() => expect(localRunner.status).toHaveBeenCalled());
  expect(localRunner.start).not.toHaveBeenCalled();
});

it("warns when the runner executable is missing", async () => {
  vi.mocked(localRunner.status).mockResolvedValue({
    ...base,
    available: false,
  });
  mount();
  expect(await screen.findByText(/未找到 niucai-runner.exe/)).toBeTruthy();
  expect(
    (screen.getByText("把本机加为节点") as HTMLButtonElement).disabled,
  ).toBe(true);
});
