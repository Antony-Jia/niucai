import { describe, it, expect, vi, afterEach } from "vitest";
import { invoke } from "@tauri-apps/api/core";
import { api, changeComputerControl, setDemo } from "../src/lib/api";

vi.mock("@tauri-apps/api/core", () => ({
  isTauri: () => true,
  invoke: vi.fn(),
}));
afterEach(() => vi.restoreAllMocks());

describe("computer hand-back", () => {
  it("reaches Kernel even when closing the native desktop fails", async () => {
    setDemo(false);
    const control = vi
      .spyOn(api, "computerControl")
      .mockResolvedValue({ control: "AGENT" } as never);
    vi.mocked(invoke).mockRejectedValue(new Error("broken desktop"));
    const warn = vi.fn();
    expect(await changeComputerControl("computer", "hand-back", warn)).toEqual({
      control: "AGENT",
    });
    expect(control).toHaveBeenCalledWith("computer", "hand-back");
    await vi.waitFor(() => expect(warn).toHaveBeenCalled());
  });

  it("does not close the desktop when Kernel rejects the transfer", async () => {
    vi.mocked(invoke).mockClear();
    vi.spyOn(api, "computerControl").mockRejectedValue(new Error("offline"));
    await expect(
      changeComputerControl("computer", "hand-back", vi.fn()),
    ).rejects.toThrow("offline");
    expect(invoke).not.toHaveBeenCalled();
  });
});
