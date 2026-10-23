import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import {
  WorkflowGraph,
  layoutStages,
  type GraphItem,
  type GraphStage,
} from "../src/components/WorkflowGraph";

const stages: GraphStage[] = [
  { key: "a", agent_id: "crawler", work_item_id: "w1", depends_on: [] },
  { key: "b", agent_id: "crawler", work_item_id: "w2", depends_on: [] },
  { key: "c", agent_id: "analyst", work_item_id: "w3", depends_on: ["a", "b"] },
];
const item = (id: string, status: string): GraphItem => ({
  id,
  status,
  goal: `goal ${id}`,
  assignee_agent_id: "x",
  conversation_id: `conv-${id}`,
});

describe("WorkflowGraph", () => {
  it("layers stages by longest dependency path", () => {
    const nodes = layoutStages(stages, []);
    expect(nodes.map((n) => [n.key, n.layer, n.row])).toEqual([
      ["a", 0, 0],
      ["b", 0, 1],
      ["c", 1, 0],
    ]);
  });
  it("shows per-stage status, progress and opens the stage conversation", () => {
    const onOpen = vi.fn();
    render(
      <WorkflowGraph
        stages={stages}
        items={[
          item("w1", "COMPLETED"),
          item("w2", "RUNNING"),
          item("w3", "WAITING_BOT"),
        ]}
        botName={(id) => id.toUpperCase()}
        nodeLabel={() => "Kernel 本机"}
        onOpen={onOpen}
      />,
    );
    expect(screen.getByText("完成 1 / 3 个阶段")).toBeInTheDocument();
    expect(screen.getByText("已完成")).toBeInTheDocument();
    expect(screen.getByText("执行中")).toBeInTheDocument();
    expect(screen.getByText("等待上游")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /^c/ }));
    expect(onOpen).toHaveBeenCalledWith("conv-w3");
  });
});
