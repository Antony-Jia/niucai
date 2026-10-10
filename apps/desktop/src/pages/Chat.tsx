import { useEffect, useRef, useState } from "react";
import { ArrowUp, Plus } from "lucide-react";
import { api } from "../lib/api";
import { useApp, useCommand, useData } from "../lib/state";
import { ConnectionEmpty, ErrorNotice, Badge } from "../components/ui";
import { TaskDetail } from "./Tasks";

export function Chat({
  conversationId,
  onConversation,
  computerId,
  compact = false,
}: {
  conversationId?: string | null;
  onConversation?: (id: string | null) => void;
  computerId?: string;
  compact?: boolean;
}) {
  const { connected, synchronizing } = useApp();
  const conversations = useData(["conversations"], api.conversations);
  const computers = useData(["computers"], api.computers);
  const [localId, setLocalId] = useState<string | null>(null);
  const id = conversationId === undefined ? localId : conversationId;
  const select = (value: string | null) =>
    onConversation ? onConversation(value) : setLocalId(value);
  const [content, setContent] = useState("");
  const [selectedComputer, setComputer] = useState<string | undefined>();
  const executionComputer =
    selectedComputer ??
    computerId ??
    computers.data?.find((c) => c.kind === "linux")?.id;
  const timeline = useData(["timeline", id], () =>
    id ? api.timeline(id) : Promise.resolve(null),
  );
  const bottom = useRef<HTMLDivElement>(null);
  const retry = useRef<
    | {
        content: string;
        id: string | null;
        clientId: string;
        createdId?: string;
      }
    | undefined
  >(undefined);
  const send = useCommand(async (text: string) => {
    if (
      !retry.current ||
      retry.current.content !== text ||
      retry.current.id !== id
    )
      retry.current = { content: text, id, clientId: crypto.randomUUID() };
    const attempt = retry.current;
    const target =
      id ||
      attempt.createdId ||
      (await api.createConversation(executionComputer)).id;
    attempt.createdId = target;
    const result = await api.sendMessage(target, text, attempt.clientId);
    return result;
  });
  useEffect(() => {
    bottom.current?.scrollIntoView?.({ behavior: "smooth" });
  }, [timeline.data?.items.length]);
  const submit = () => {
    const text = content.trim();
    if (!text || send.isPending) return;
    send.mutate(text, {
      onSuccess: (result) => {
        select(result.conversation.id);
        setContent((current) => (current.trim() === text ? "" : current));
        retry.current = undefined;
      },
    });
  };
  if (!connected) return <ConnectionEmpty />;
  return (
    <div className={`unified-chat ${compact ? "compact" : ""}`}>
      {!compact && (
        <aside className="conversation-list panel">
          <button onClick={() => select(null)}>
            <Plus size={15} /> 新建会话
          </button>
          {conversations.data?.map((c) => (
            <button
              key={c.id}
              className={id === c.id ? "active" : ""}
              onClick={() => select(c.id)}
            >
              {c.title}
            </button>
          ))}
          <ErrorNotice error={conversations.error} />
        </aside>
      )}
      <section className="conversation-main" aria-label="统一会话">
        <div className="session-scroll">
          <ErrorNotice error={timeline.error} />
          {!id && (
            <div className="workspace-empty">
              <h2>你想一起完成什么？</h2>
              <p>讨论、执行与后续要求，都在这个会话里继续。</p>
            </div>
          )}
          {timeline.data?.items.map((item) =>
            item.message ? (
              <div key={item.id} className={`message ${item.message.role}`}>
                <div className="message-label">
                  {item.message.role === "user" ? "你" : "niucai"}
                </div>
                <p>{item.message.content || "回复已中断"}</p>
                {item.message.status === "INTERRUPTED" && (
                  <small>历史回复已中断</small>
                )}
              </div>
            ) : /^(task\.(created|started|completed|pending|failed|waiting_human|pause|resume|cancel|retry)|action\.(executing|succeeded|failed|unknown|denied)|approval\.requested)$/.test(
                item.type,
              ) ? (
              <div className="timeline-event" key={item.id}>
                <span>
                  {item.type.startsWith("action.")
                    ? "工具动作"
                    : item.type.startsWith("approval.")
                      ? "等待审批"
                      : "执行进度"}
                </span>
                <Badge
                  status={String(
                    item.data.status || item.type.split(".")[1].toUpperCase(),
                  )}
                />
                {!!item.data.reason && (
                  <small>{String(item.data.reason)}</small>
                )}
              </div>
            ) : null,
          )}
          {timeline.data?.tasks.map((task) => (
            <details
              className="conversation-execution"
              key={task.id}
              open={!["COMPLETED", "CANCELLED"].includes(task.status)}
            >
              <summary>
                {task.title} <Badge status={task.status} />
              </summary>
              <TaskDetail
                task={task}
                stale={timeline.isError || synchronizing}
              />
            </details>
          ))}
          <div ref={bottom} />
        </div>
        <form
          className="workspace-composer"
          onSubmit={(e) => {
            e.preventDefault();
            submit();
          }}
        >
          {!id && (
            <label>
              执行电脑{" "}
              <select
                aria-label="会话执行电脑"
                value={executionComputer || ""}
                onChange={(e) => setComputer(e.target.value)}
              >
                <option value="">不使用电脑</option>
                {computers.data
                  ?.filter((c) => c.kind === "linux")
                  .map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name}
                    </option>
                  ))}
              </select>
            </label>
          )}
          <textarea
            aria-label="聊天内容"
            placeholder="发送目标或补充要求… Ctrl + Enter 发送"
            value={content}
            maxLength={12000}
            onChange={(e) => setContent(e.target.value)}
            onKeyDown={(e) => {
              if (e.ctrlKey && e.key === "Enter") {
                e.preventDefault();
                submit();
              }
            }}
          />
          <div>
            <span>消息与执行由服务器持久保存</span>
            <button
              className="primary"
              aria-label="发送消息"
              disabled={send.isPending || !content.trim()}
            >
              <ArrowUp size={16} />
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}
