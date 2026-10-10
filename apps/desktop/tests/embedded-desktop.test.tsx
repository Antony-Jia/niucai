import { cleanup } from "@testing-library/react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { EmbeddedDesktop } from "../src/components/EmbeddedDesktop";
import { embedComputer, closeComputer } from "../src/lib/api";
const { notify } = vi.hoisted(() => ({ notify: vi.fn() }));
vi.mock("../src/lib/api", async (original) => ({
  ...(await original<typeof import("../src/lib/api")>()),
  embedComputer: vi.fn(),
  closeComputer: vi.fn(),
}));
vi.mock("../src/lib/state", async (original) => ({
  ...(await original<typeof import("../src/lib/state")>()),
  useApp: () => ({ notify }),
}));
beforeEach(() => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
  vi.spyOn(Element.prototype, "getBoundingClientRect").mockImplementation(
    function (this: Element) {
      return {
        x: 400,
        y: this.classList.contains("embedded-desktop-viewport") ? 240 : 120,
        width: 700,
        height: 500,
        top: 240,
        left: 400,
        right: 1100,
        bottom: 740,
        toJSON() {},
      };
    },
  );
  vi.mocked(embedComputer).mockReset().mockResolvedValue(undefined);
  vi.mocked(closeComputer).mockReset().mockResolvedValue(undefined);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});
describe("embedded desktop recovery", () => {
  it("keeps controls outside the native viewport and closes before reconnecting", async () => {
    const onClose = vi.fn();
    render(<EmbeddedDesktop computerId="remote-main" onClose={onClose} />);
    await waitFor(() => expect(embedComputer).toHaveBeenCalled());
    expect(embedComputer).toHaveBeenLastCalledWith("remote-main", {
      x: 400,
      y: 240,
      width: 700,
      height: 500,
      visible: true,
    });
    const retry = screen.getByRole("button", { name: "重新连接桌面" });
    expect(screen.getByLabelText("远程桌面画面").contains(retry)).toBe(false);
    fireEvent.click(retry);
    await waitFor(() => expect(embedComputer).toHaveBeenCalledTimes(2));
    expect(closeComputer).toHaveBeenCalled();
    expect(vi.mocked(closeComputer).mock.invocationCallOrder[0]).toBeLessThan(
      vi.mocked(embedComputer).mock.invocationCallOrder[1],
    );
    fireEvent.click(screen.getByRole("button", { name: "关闭桌面连接" }));
    expect(onClose).toHaveBeenCalledOnce();
  });
  it("removes a failed native view and allows retry", async () => {
    vi.mocked(embedComputer).mockRejectedValueOnce(new Error("HTTP ERROR 401"));
    render(<EmbeddedDesktop computerId="remote-main" onClose={vi.fn()} />);
    await waitFor(() => expect(closeComputer).toHaveBeenCalled());
    expect(screen.getByRole("button", { name: "关闭桌面连接" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "重新连接桌面" }));
    await waitFor(() => expect(embedComputer).toHaveBeenCalledTimes(2));
  });
});
