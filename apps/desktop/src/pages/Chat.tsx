import { useState, useEffect, useRef } from "react";
import {
  ArrowUp,
  MessageSquare,
  Plus,
  Sparkles,
  ArrowRight,
} from "lucide-react";
import { api } from "../lib/api";
import { useApp, useCommand, useData } from "../lib/state";
import {
  ConnectionEmpty,
  Empty,
  ErrorNotice,
  Header,
  time,
} from "../components/ui";
export function Chat() {
  const { connected, setPage } = useApp();
  const conversations = useData(["conversations"], api.conversations);
  const computers = useData(["computers"], api.computers);
  const [computerId, setComputerId] = useState<string | undefined>();
  const executionComputer =
    computerId ?? computers.data?.find((c) => c.kind === "linux")?.id ?? "";
  const [conversation, setConversation] = useState<string | undefined>();
  const [content, setContent] = useState("");
  const [taskGoal, setTaskGoal] = useState("");
  const messages = useData(["messages", conversation], () =>
    conversation ? api.messages(conversation) : Promise.resolve([]),
  );
  const bottom = useRef<HTMLDivElement>(null);
  const send = useCommand((text: string) => api.chat(text, conversation));
  const create = useCommand(
    (goal: string) =>
      api.createTask(goal.slice(0, 70), goal, executionComputer),
    "任务已创建",
  );
  useEffect(() => {
    bottom.current?.scrollIntoView?.({ behavior: "smooth" });
  }, [messages.data]);
  function submit() {
    const text = content.trim();
    if (!text || send.isPending) return;
    send.mutate(text, {
      onSuccess: (r) => {
        setConversation(r.conversation.id);
        setContent("");
        setTaskGoal(text);
      },
    });
  }
  return (
    <>
      <Header
        eyebrow="THINK TOGETHER"
        title="先聊清楚，再开始。"
        description="讨论想法与目标。只有明确交给 Agent 的内容，才会成为任务。"
      />
      {!connected ? (
        <ConnectionEmpty />
      ) : (
        <div className="chat-layout">
          <aside className="panel conversations">
            <button
              className="full-width"
              disabled={send.isPending}
              onClick={() => {
                setConversation(undefined);
                setTaskGoal("");
              }}
            >
              <Plus size={16} />
              新对话
            </button>
            {conversations.data?.map((c) => (
              <button
                className={
                  c.id === conversation ? "conversation active" : "conversation"
                }
                key={c.id}
                disabled={send.isPending}
                onClick={() => {
                  setConversation(c.id);
                  setTaskGoal("");
                }}
              >
                <MessageSquare size={15} />
                <span>{c.title}</span>
              </button>
            ))}
          </aside>
          <section className="panel chat-main">
            <label className="chat-computer-choice">
              转交 Agent 的执行电脑
              <select
                aria-label="聊天任务执行电脑"
                value={executionComputer}
                disabled={computers.isLoading || create.isPending}
                onChange={(e) => setComputerId(e.target.value)}
              >
                <option value="">不使用电脑（纯推理任务）</option>
                {computers.data
                  ?.filter((c) => c.kind === "linux")
                  .map((c) => (
                    <option value={c.id} key={c.id}>
                      {c.name}
                    </option>
                  ))}
              </select>
            </label>
            <ErrorNotice error={computers.error} />
            <ErrorNotice error={messages.error} />
            <div className="messages">
              {!conversation && (
                <Empty
                  title="有什么想一起完成的？"
                  description="从一个想法、一项研究，或一个电脑任务开始。"
                />
              )}
              {messages.data?.map((m) => (
                <div className={`message ${m.role}`} key={m.id}>
                  <div className="message-label">
                    {m.role === "assistant" ? (
                      <>
                        <Sparkles size={14} />
                        niucai
                      </>
                    ) : (
                      "你"
                    )}
                    <small>{time(m.created_at)}</small>
                  </div>
                  <p className="preserve">{m.content}</p>
                  {m.status === "PENDING" && (
                    <span className="badge waiting_human">
                      回复未完成；若服务器曾重启，请新建会话继续
                    </span>
                  )}
                  {m.status === "FAILED" && (
                    <span className="badge failed">模型暂不可用</span>
                  )}
                  {m.role === "user" && (
                    <button
                      className="text-button"
                      disabled={
                        create.isPending ||
                        computers.isLoading ||
                        !!computers.error
                      }
                      onClick={() =>
                        create.mutate(m.content, {
                          onSuccess: () => setPage("tasks"),
                        })
                      }
                    >
                      交给 Agent 执行 <ArrowRight size={13} />
                    </button>
                  )}
                </div>
              ))}
              {send.isPending && (
                <div className="message assistant">
                  <span className="thinking">
                    正在思考<span>…</span>
                  </span>
                </div>
              )}
              <div ref={bottom} />
            </div>
            <form
              className="composer"
              onSubmit={(e) => {
                e.preventDefault();
                submit();
              }}
            >
              <textarea
                aria-label="聊天内容"
                placeholder="描述你的想法…  Ctrl + Enter 发送"
                value={content}
                maxLength={12000}
                onChange={(e) => setContent(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && e.ctrlKey) {
                    e.preventDefault();
                    submit();
                  }
                }}
              />
              <div className="composer-footer">
                <span>
                  {taskGoal
                    ? "对话与任务分别保存"
                    : "任务可在关闭客户端后继续运行"}
                </span>
                <button
                  className="primary icon-button"
                  aria-label="发送消息"
                  disabled={send.isPending || !content.trim()}
                >
                  <ArrowUp size={18} />
                </button>
              </div>
            </form>
          </section>
        </div>
      )}
    </>
  );
}
