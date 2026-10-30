import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus } from "lucide-react";
import {
  Header,
  ErrorNotice,
  Loading,
  ConnectionEmpty,
  Modal,
  time,
} from "../components/ui";
import { useApp } from "../lib/state";
import {
  boardApi,
  boardColumns,
  cardOperations,
  needsBody,
  operationText,
  splitList,
  statusText,
  type Board,
  type Card,
  type CardDetail,
  type CardOperation,
} from "../lib/board";

const eventText: Record<string, string> = {
  status: "状态",
  progress: "进度",
  comment: "评论",
  question: "提问",
  answer: "回答",
  proposal: "提议",
  submit: "提交",
};

function errorText(e: unknown) {
  return e instanceof Error ? e.message : String(e);
}

function NewCardForm({
  board,
  onClose,
}: {
  board: Board;
  onClose: () => void;
}) {
  const { notify } = useApp();
  const cache = useQueryClient();
  const [title, setTitle] = useState("");
  const [goal, setGoal] = useState("");
  const [acceptance, setAcceptance] = useState("");
  const [assignee, setAssignee] = useState("");
  const [tags, setTags] = useState("");
  const [depends, setDepends] = useState<string[]>([]);
  const [review, setReview] = useState<"none" | "llm">("none");
  const [busy, setBusy] = useState(false);
  const bots = board.bots.filter((b) => b.enabled);
  async function submit(draft: boolean) {
    setBusy(true);
    try {
      await boardApi.createCard({
        title: title.trim(),
        goal,
        acceptance,
        assignee_bot_id: assignee || null,
        required_tags: splitList(tags),
        depends_on: depends,
        priority: 0,
        review,
        draft,
      });
      await cache.invalidateQueries({ queryKey: ["board"] });
      onClose();
    } catch (e) {
      notify(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal title="新建卡片" onClose={onClose}>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void submit(false);
        }}
      >
        <label>
          标题
          <input
            required
            maxLength={200}
            value={title}
            onChange={(e) => setTitle(e.target.value)}
          />
        </label>
        <label>
          目标
          <textarea
            rows={4}
            value={goal}
            onChange={(e) => setGoal(e.target.value)}
          />
        </label>
        <label>
          验收标准
          <textarea
            rows={2}
            value={acceptance}
            onChange={(e) => setAcceptance(e.target.value)}
          />
        </label>
        <label>
          负责 Bot
          <select
            value={assignee}
            onChange={(e) => setAssignee(e.target.value)}
            disabled={bots.length === 0}
          >
            <option value="">
              {bots.length ? "公共池（按标签领取）" : "无可用bot"}
            </option>
            {bots.map((b) => (
              <option key={b.id} value={b.id}>
                @{b.name} · {b.engine}
              </option>
            ))}
          </select>
        </label>
        {!assignee && (
          <label>
            需要的标签
            <input
              value={tags}
              onChange={(e) => setTags(e.target.value)}
              placeholder="例如：frontend, docs"
            />
          </label>
        )}
        {board.cards.length > 0 && (
          <label>
            依赖卡片（完成后其产物作为输入）
            <select
              multiple
              value={depends}
              onChange={(e) =>
                setDepends(Array.from(e.target.selectedOptions, (o) => o.value))
              }
            >
              {board.cards
                .filter((c) => c.status !== "CANCELLED")
                .map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.title}
                  </option>
                ))}
            </select>
          </label>
        )}
        <label>
          验收方式
          <select
            value={review}
            onChange={(e) => setReview(e.target.value as "none" | "llm")}
          >
            <option value="none">由你验收</option>
            <option value="llm">LLM 先审一遍</option>
          </select>
        </label>
        <div className="button-row">
          <button
            type="button"
            disabled={busy || !title.trim()}
            onClick={() => void submit(true)}
          >
            存为草稿
          </button>
          <button className="primary" disabled={busy || !title.trim()}>
            发布
          </button>
        </div>
      </form>
    </Modal>
  );
}

function AttemptEvents({ jobId }: { jobId: string }) {
  const events = useQuery({
    queryKey: ["job-events", jobId],
    queryFn: () => boardApi.jobEvents(jobId),
  });
  if (events.isLoading) return <Loading />;
  return (
    <ol className="attempt-events">
      {events.data?.slice(-30).map((e) => {
        const payload = e.data.payload || {};
        const text = String(
          payload.text ?? payload.name ?? payload.error ?? "",
        ).slice(0, 300);
        return (
          <li key={e.id}>
            <small>{time(e.created_at)}</small> {e.data.kind || e.type}
            {text && ` · ${text}`}
          </li>
        );
      })}
      {events.data?.length === 0 && <li className="muted">暂无事件</li>}
    </ol>
  );
}

export function CardDetailView({
  id,
  onClose,
  readOnly = false,
}: {
  id: string;
  onClose: () => void;
  /** 只读查看：隐藏批准、验收、取消等操作和评论框（工作地图使用）。 */
  readOnly?: boolean;
}) {
  const { notify, demo } = useApp();
  const cache = useQueryClient();
  const detail = useQuery({
    queryKey: ["card", id],
    queryFn: () => boardApi.card(id),
  });
  const [body, setBody] = useState("");
  const [comment, setComment] = useState("");
  const [openAttempt, setOpenAttempt] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function run(action: () => Promise<unknown>) {
    setBusy(true);
    try {
      await action();
      await Promise.all([
        cache.invalidateQueries({ queryKey: ["board"] }),
        cache.invalidateQueries({ queryKey: ["goal"] }),
        cache.invalidateQueries({ queryKey: ["card", id] }),
      ]);
      return true;
    } catch (e) {
      notify(errorText(e));
      return false;
    } finally {
      setBusy(false);
    }
  }
  const card: CardDetail | undefined = detail.data;
  return (
    <Modal title={card?.title || "卡片"} onClose={onClose}>
      <ErrorNotice error={detail.error} />
      {card && (
        <div className="card-detail">
          <p>
            <span className={`badge card-${card.status.toLowerCase()}`}>
              {statusText[card.status]}
            </span>{" "}
            {card.assignee_name ? `@${card.assignee_name}` : "公共池"} · 第{" "}
            {card.attempts}/{card.budget.max_attempts ?? 3} 次尝试
          </p>
          {card.goal && <p className="card-goal">{card.goal}</p>}
          {card.acceptance && (
            <p className="muted">验收标准：{card.acceptance}</p>
          )}
          {card.result.summary && (
            <section>
              <h3>提交结果</h3>
              <p>{card.result.summary}</p>
              {card.result.files?.length ? (
                <small className="muted">
                  文件：{card.result.files.map((f) => f.path).join("、")}
                </small>
              ) : null}
            </section>
          )}
          {!readOnly && (
            <div className="card-actions">
              {cardOperations(card.status).some(needsBody) && (
                <textarea
                  rows={2}
                  aria-label="回答或退回意见"
                  placeholder={
                    card.status === "BLOCKED" ? "回答 Bot 的问题" : "退回原因"
                  }
                  value={body}
                  onChange={(e) => setBody(e.target.value)}
                />
              )}
              <div className="button-row">
                {cardOperations(card.status).map((op: CardOperation) => (
                  <button
                    key={op}
                    className={
                      op === "cancel" || op === "reject" ? "" : "primary"
                    }
                    disabled={busy || demo || (needsBody(op) && !body.trim())}
                    onClick={() => {
                      if (op === "cancel" && !confirm("取消这张卡片？")) return;
                      void run(() =>
                        boardApi.action(card.id, op, card.version, body),
                      ).then((ok) => ok && setBody(""));
                    }}
                  >
                    {operationText[op]}
                  </button>
                ))}
              </div>
            </div>
          )}
          <section>
            <h3>动态</h3>
            <ol className="card-timeline">
              {card.events.map((e) => (
                <li key={e.id} className={`card-event ${e.kind}`}>
                  <small>
                    {time(e.created_at)} · {e.actor} ·{" "}
                    {eventText[e.kind] || e.kind}
                  </small>
                  <p>{e.body || String(e.data.to ?? "")}</p>
                </li>
              ))}
            </ol>
            {!readOnly && (
              <form
                className="card-comment"
                onSubmit={(e) => {
                  e.preventDefault();
                  void run(() => boardApi.comment(card.id, comment)).then(
                    (ok) => ok && setComment(""),
                  );
                }}
              >
                <input
                  aria-label="评论"
                  placeholder="评论，可用 @Bot名 提及"
                  value={comment}
                  onChange={(e) => setComment(e.target.value)}
                />
                <button disabled={busy || demo || !comment.trim()}>发送</button>
              </form>
            )}
          </section>
          {card.runs.length > 0 && (
            <section>
              <h3>执行记录</h3>
              {card.runs.map((a) => (
                <div key={a.id} className="attempt">
                  <button
                    className="link-button"
                    onClick={() =>
                      setOpenAttempt(openAttempt === a.id ? null : a.id)
                    }
                  >
                    {time(a.created_at)} · {a.engine} · {a.status}
                    {a.error ? ` · ${a.error}` : ""}
                  </button>
                  {openAttempt === a.id && <AttemptEvents jobId={a.id} />}
                </div>
              ))}
            </section>
          )}
        </div>
      )}
    </Modal>
  );
}

function CardTile({ card, onOpen }: { card: Card; onOpen: () => void }) {
  return (
    <button className="kanban-card board-card" onClick={onOpen}>
      <strong>{card.title}</strong>
      <span className={`badge card-${card.status.toLowerCase()}`}>
        {statusText[card.status]}
      </span>
      <small>
        {card.assignee_name ? `@${card.assignee_name}` : "公共池"}
        {card.required_tags.length > 0 &&
          ` · #${card.required_tags.join(" #")}`}
      </small>
      {card.latest && <small className="muted">{card.latest.body}</small>}
    </button>
  );
}

/** 按状态分列的卡片墙；白板页与 Goal 页共用。 */
export function BoardColumns({
  cards,
  onOpen,
}: {
  cards: Card[];
  onOpen: (id: string) => void;
}) {
  return (
    <div className="kanban-board">
      {boardColumns.map((column) => {
        const items = cards.filter((c) => column.statuses.includes(c.status));
        return (
          <section key={column.key} className="kanban-column">
            <h3>
              {column.label}
              <span>{items.length}</span>
            </h3>
            {items.map((c) => (
              <CardTile key={c.id} card={c} onOpen={() => onOpen(c.id)} />
            ))}
          </section>
        );
      })}
    </div>
  );
}

export function Whiteboard() {
  const { connected, demo } = useApp();
  const board = useQuery({
    queryKey: ["board"],
    queryFn: boardApi.board,
    enabled: connected,
    refetchInterval: 5000,
  });
  const [creating, setCreating] = useState(false);
  const [open, setOpen] = useState<string | null>(null);
  const data = board.data;
  if (!connected && !demo) return <ConnectionEmpty />;
  return (
    <>
      <Header
        eyebrow="WHITEBOARD"
        title="白板"
        description="把工作写成卡片，节点上的 Bot 领取执行；需要你回答或验收的卡片会出现在“需要你”。"
      >
        <button
          className="primary"
          disabled={!data || demo}
          onClick={() => setCreating(true)}
        >
          <Plus size={15} />
          新建卡片
        </button>
      </Header>
      <ErrorNotice error={board.error} />
      {board.isLoading && <Loading />}
      {data && data.bots.filter((b) => b.enabled).length === 0 && (
        <p className="quiet-note">
          无可用bot：在“设置 → 节点”里为节点添加 Bot 后，卡片才会被领取。
        </p>
      )}
      {data && <BoardColumns cards={data.cards} onOpen={setOpen} />}
      {creating && data && (
        <NewCardForm board={data} onClose={() => setCreating(false)} />
      )}
      {open && <CardDetailView id={open} onClose={() => setOpen(null)} />}
    </>
  );
}
