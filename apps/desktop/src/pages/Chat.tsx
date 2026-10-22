import { useEffect, useRef, useState } from "react";
import { ArrowUp, Plus } from "lucide-react";
import { api, request } from "../lib/api";
import type { Bot } from "./Bots";
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
  const bots = useData(["bots"], () => request<Bot[]>("/api/agents"));
  const collaboration = useData(["collaboration-status"], () =>
    request<{ enabled: boolean }>("/api/collaboration/status"),
  );
  const [agentId, setAgentId] = useState("main");
  const [groupIds, setGroupIds] = useState<string[]>(["main"]);
  const [localId, setLocalId] = useState<string | null>(() => {
    const requested = sessionStorage.getItem("niucai.open-conversation");
    sessionStorage.removeItem("niucai.open-conversation");
    return requested;
  });
  const id = conversationId === undefined ? localId : conversationId;
  const participants = useData(["participants", id], () =>
    id
      ? request<{ agent_id: string }[]>(`/api/conversations/${id}/participants`)
      : Promise.resolve([]),
  );
  const select = (value: string | null) =>
    onConversation ? onConversation(value) : setLocalId(value);
  const [content, setContent] = useState("");
  const [selectedComputer, setComputer] = useState<string | undefined>();
  const executionComputer =
    selectedComputer ??
    computerId ??
    (collaboration.data?.enabled
      ? undefined
      : computers.data?.find((c) => c.kind === "linux")?.id);
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
    let targetComputer = selectedComputer ?? computerId;
    if (!id && !attempt.createdId && !targetComputer) {
      const status =
        collaboration.data ??
        (await request<{ enabled: boolean }>("/api/collaboration/status"));
      if (!status.enabled) {
        const available = computers.data ?? (await api.computers());
        targetComputer = available.find((c) => c.kind === "linux")?.id;
      }
    }
    const target =
      id ||
      attempt.createdId ||
      (await api.createConversation(targetComputer, agentId, groupIds)).id;
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
        {!id && (
          <label>
            负责 Bot{" "}
            <select
              value={agentId}
              onChange={(e) => {
                setAgentId(e.target.value);
                setGroupIds((current) => [
                  ...new Set([...current, e.target.value]),
                ]);
              }}
            >
              {bots.data
                ?.filter(
                  (b) =>
                    !b.definition.archived && b.definition.enabled !== false,
                )
                .map((b) => (
                  <option key={b.id} value={b.id}>
                    {b.name}
                  </option>
                ))}
            </select>
          </label>
        )}
        {!id && collaboration.data?.enabled && (
          <fieldset>
            <legend>群聊成员（可选，最多 6 位）</legend>
            {bots.data
              ?.filter(
                (b) => !b.definition.archived && b.definition.enabled !== false,
              )
              .map((b) => (
                <label key={b.id} style={{ marginRight: 12 }}>
                  <input
                    type="checkbox"
                    disabled={b.id === agentId}
                    checked={groupIds.includes(b.id)}
                    onChange={(e) =>
                      setGroupIds((current) =>
                        e.target.checked
                          ? [...current, b.id]
                          : current.filter((value) => value !== b.id),
                      )
                    }
                  />
                  {b.name}
                </label>
              ))}
          </fieldset>
        )}
        {content.endsWith("@") && (
          <div>
            {[
              ...(participants.data?.map((p) => p.agent_id) ?? []),
              "everyone",
            ].map((mention) => (
              <button
                key={mention}
                onClick={() => setContent((value) => value + mention + " ")}
              >
                @{mention}
              </button>
            ))}
          </div>
        )}
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
                  {item.message.role === "user"
                    ? "你"
                    : bots.data?.find(
                    (b) => b.id === item.message?.sender_agent_id,
                      )?.name ||
                      item.message.sender_agent_id ||
                      "niucai"}
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
