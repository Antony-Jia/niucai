import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import { ComputerPreview } from "../src/components/ComputerPreview";
import { api } from "../src/lib/api";

afterEach(() => vi.restoreAllMocks());

it("does not request hidden frames and refreshes when the page becomes visible", async () => {
  let visibility = "hidden";
  vi.spyOn(document, "visibilityState", "get").mockImplementation(
    () => visibility as DocumentVisibilityState,
  );
  const capture = vi.spyOn(api, "computerPreview").mockResolvedValue({
    image: "data:image/jpeg;base64,AA==",
    captured_at: new Date(0).toISOString(),
  });
  const cache = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const view = render(
    <QueryClientProvider client={cache}>
      <ComputerPreview computerId="one" />
    </QueryClientProvider>,
  );
  expect(capture).not.toHaveBeenCalled();
  visibility = "visible";
  fireEvent(document, new Event("visibilitychange"));
  await screen.findByAltText("远程浏览器只读画面");
  expect(screen.getByText("以下为上次画面，正在等待更新")).toBeInTheDocument();
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "刷新画面" })).toBeEnabled(),
  );
  const calls = capture.mock.calls.length;
  fireEvent.click(screen.getByRole("button", { name: "刷新画面" }));
  await waitFor(() => expect(capture.mock.calls.length).toBeGreaterThan(calls));
  visibility = "hidden";
  fireEvent(document, new Event("visibilitychange"));
  expect(screen.getByText("页面隐藏，已暂停刷新")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "刷新画面" })).toBeDisabled();
  view.unmount();
  cache.clear();
});
