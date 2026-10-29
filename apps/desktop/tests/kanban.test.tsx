import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { expect, it, vi } from "vitest";
import { Kanban } from "../src/pages/Kanban";
import { request } from "../src/lib/api";

vi.mock("../src/lib/state", () => ({
  useApp: () => ({ connected: true, demo: false }),
  queryClient: new QueryClient(),
}));
vi.mock("../src/lib/api", () => ({ request: vi.fn(), setDemo: vi.fn() }));

it("shows tasks with their agents and which VPS is busy", async () => {
  vi.mocked(request).mockResolvedValue({
    tasks: [
      {
        id: "t1",
        title: "Fix login",
        status: "RUNNING",
        column: "RUNNING",
        updated_at: "",
        agents: [{ id: "main", name: "Main Agent", role: "lead" }],
        remote_jobs: [
          { id: "j1", engine: "codex", status: "RUNNING", node_name: "vps-1" },
        ],
      },
    ],
    resources: [
      {
        id: "n1",
        name: "vps-1",
        kind: "node",
        status: "ONLINE",
        busy: true,
        job: { engine: "codex", task_title: "Fix login", prompt: "go" },
      },
    ],
  } as never);
  render(
    <QueryClientProvider client={new QueryClient()}>
      <Kanban />
    </QueryClientProvider>,
  );
  expect(await screen.findByText("Main Agent · 主责")).toBeTruthy();
  expect(screen.getByText("远端 codex · vps-1 · RUNNING")).toBeTruthy();
  expect(screen.getByText("忙碌")).toBeTruthy();
});
