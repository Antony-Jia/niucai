import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, RefreshCw, Send } from "lucide-react";
import {
  ConnectionEmpty,
  Empty,
  ErrorNotice,
  Header,
  Loading,
  Modal,
  time,
} from "../components/ui";
import { useApp } from "../lib/state";
import {
  goalsApi,
  triggerText,
  type GoalDetail,
  type OrchestratorRun,
} from "../lib/goals";
import { BoardColumns, CardDetailView } from "./Whiteboard";

function errorText(e: unknown) {
  return e instanceof Error ? e.message : String(e);
}

const messageLabel: Record<string, string> = {
  QUESTION: "@kernel 提问",
  FINAL: "@kernel 总结",
  REPLY: "@kernel",
};

function RunSteps({ id }: { id: string }) {
  const run = useQuery({
    queryKey: ["orchestrator-run", id],
    queryFn: () => goalsApi.run(id),
  });
  if (run.isLoading) return <Loading />;
  return (
    <ol className="attempt-events">
      {run.data?.steps?.map((step, i) => (
        <li key={i}>
          <strong>{step.tool}</strong> {JSON.stringify(step.args)}
          <br />
          <small className="muted">{step.result.slice(0, 400)}</small>
        </li>
      ))}
      {run.data?.output && <li className="muted">{run.data.output}</li>}
      {run.data?.error && <li className="error-text">{run.data.error}</li>}
    </ol>
  );
}

function Runs({ runs }: { runs: OrchestratorRun[] }) {
  const [open, setOpen] = useState<string | null>(null);
  if (runs.length === 0) return null;
  return (
    <section className="panel goal-runs">
      <h3>编排记录</h3>
      {runs.map((r) => (
        <div key={r.id} className="attempt">
          <button
            className="link-button"
            onClick={() => setOpen(open === r.id ? null : r.id)}
          >
            {time(r.created_at)} · {r.status} ·{" "}
            {r.triggers.map((t) => triggerText[t.kind] || t.kind).join("、")}
            {r.tool_calls ? ` · ${r.tool_calls} 次工具调用` : ""}
          </button>
          {open === r.id && <RunSteps id={r.id} />}
        </div>
      ))}
    </section>
  );
}

function GoalView({ id }: { id: string }) {
  const { notify, demo } = useApp();
  const cache = useQueryClient();
  const goal = useQuery({
    queryKey: ["goal", id],
    queryFn: () => goalsApi.get(id),
    refetchInterval: 5000,
  });
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [card, setCard] = useState<string | null>(null);
  async function run(action: () => Promise<unknown>) {
    setBusy(true);
    try {
      await action();
      await cache.invalidateQueries({ queryKey: ["goal", id] });
      await cache.invalidateQueries({ queryKey: ["goals"] });
      return true;
    } catch (e) {
      notify(errorText(e));
      return false;
    } finally {
      setBusy(false);
    }
  }
  const data: GoalDetail | undefined = goal.data;
  if (goal.isLoading) return <Loading />;
  if (!data) return <ErrorNotice error={goal.error} />;
  const working = data.runs.some(
    (r) => r.status === "PENDING" || r.status === "RUNNING",
  );
  return (
    <div className="goal-view">
      <div className="section-heading">
        <h2>{data.title}</h2>
        <span className={`badge goal-${data.status.toLowerCase()}`}>
          {data.status === "DONE" ? "已完成" : working ? "编排中…" : "进行中"}
        </span>
        <button
          disabled={busy || demo || working || !data.orchestrator_enabled}
          onClick={() => void run(() => goalsApi.rerun(id))}
          title="让 @kernel 重新看一遍白板"
        >
          <RefreshCw size={14} />
          重新编排
        </button>
      </div>
      {!data.orchestrator_enabled && (
        <p className="quiet-note">
          编排未启用：Kernel 设置 NIUCAI_ORCHESTRATOR_ENABLED=true 后 @kernel
          才会处理这个 Goal。
        </p>
      )}
      <ol className="goal-messages">
        {data.messages.map((m) => (
          <li
            key={m.id}
            className={`goal-message ${m.role === "user" ? "from-user" : "from-kernel"} ${m.kind.toLowerCase()}`}
          >
            <small>
              {m.role === "user" ? "你" : messageLabel[m.kind] || "@kernel"} ·{" "}
              {time(m.created_at)}
            </small>
            <p className="preserve">{m.content}</p>
          </li>
        ))}
      </ol>
      <form
        className="card-comment"
        onSubmit={(e) => {
          e.preventDefault();
          void run(() => goalsApi.send(id, text.trim())).then(
            (ok) => ok && setText(""),
          );
        }}
      >
        <input
          aria-label="对 @kernel 说"
          placeholder="补充要求、回答 @kernel 的问题…"
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
        <button className="primary" disabled={busy || demo || !text.trim()}>
          <Send size={14} />
          发送
        </button>
      </form>
      {data.board.bots.filter((b) => b.enabled).length === 0 && (
        <p className="quiet-note">
          无可用bot：在“设置 → 节点”里为节点添加 Bot 后，@kernel 才能派活。
        </p>
      )}
      <BoardColumns cards={data.board.cards} onOpen={setCard} />
      <Runs runs={data.runs} />
      {card && <CardDetailView id={card} onClose={() => setCard(null)} />}
    </div>
  );
}

function NewGoal({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (id: string) => void;
}) {
  const { notify } = useApp();
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  return (
    <Modal title="新建 Goal" onClose={onClose}>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          setBusy(true);
          goalsApi
            .create(message.trim())
            .then((goal) => onCreated(goal.id))
            .catch((err) => notify(errorText(err)))
            .finally(() => setBusy(false));
        }}
      >
        <label>
          目标
          <textarea
            rows={5}
            required
            maxLength={16000}
            placeholder="例如：把官网登录页的报错修好，并补上 README 说明"
            value={message}
            onChange={(e) => setMessage(e.target.value)}
          />
        </label>
        <button
          className="primary full-width"
          disabled={busy || !message.trim()}
        >
          交给 @kernel
        </button>
      </form>
    </Modal>
  );
}

export function Goals() {
  const { connected, demo } = useApp();
  const cache = useQueryClient();
  const goals = useQuery({
    queryKey: ["goals"],
    queryFn: goalsApi.list,
    enabled: connected,
    refetchInterval: 10000,
  });
  const [selected, setSelected] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  if (!connected && !demo) return <ConnectionEmpty />;
  const current = selected ?? goals.data?.[0]?.id ?? null;
  return (
    <>
      <Header
        eyebrow="GOALS"
        title="Goal"
        description="说出目标，@kernel 把它拆成白板卡片派给 Bot，并替你验收、回答和汇总。"
      >
        <button
          className="primary"
          disabled={demo}
          onClick={() => setCreating(true)}
        >
          <Plus size={15} />
          新建 Goal
        </button>
      </Header>
      <ErrorNotice error={goals.error} />
      <div className="goal-layout">
        <nav className="goal-list" aria-label="Goal 列表">
          {goals.data?.map((g) => (
            <button
              key={g.id}
              className={g.id === current ? "nav-item active" : "nav-item"}
              onClick={() => setSelected(g.id)}
            >
              <span>{g.title}</span>
              <small>
                {g.status === "DONE" ? "已完成" : "进行中"} ·{" "}
                {Object.values(g.cards).reduce((a, b) => a + b, 0)} 张卡片
              </small>
            </button>
          ))}
        </nav>
        <div className="goal-main">
          {current ? (
            <GoalView key={current} id={current} />
          ) : (
            goals.data && (
              <Empty
                title="还没有 Goal"
                description="新建一个 Goal，@kernel 会规划并分派给节点上的 Bot。"
              />
            )
          )}
        </div>
      </div>
      {creating && (
        <NewGoal
          onClose={() => setCreating(false)}
          onCreated={(id) => {
            setCreating(false);
            setSelected(id);
            void cache.invalidateQueries({ queryKey: ["goals"] });
          }}
        />
      )}
    </>
  );
}
