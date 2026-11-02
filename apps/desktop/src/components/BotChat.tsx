import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Modal, ErrorNotice, time } from "./ui";
import { useApp } from "../lib/state";
import { boardApi, statusText, type Bot, type Card } from "../lib/board";

function errorText(e: unknown) {
  return e instanceof Error ? e.message : String(e);
}

/** 私聊状态：排队、回答中、等待，其余视为空闲。 */
export function chatStatusText(status: string) {
  if (status === "READY") return "排队中，等 Bot 空出来就回答";
  if (status === "RUNNING") return "Bot 正在回答…";
  if (status === "WAITING" || status === "BLOCKED") return "Bot 在等待";
  if (status === "FAILED") return "上一条没有答成，可以重新发送";
  return "";
}

/** 一个私聊线程：消息列表 + 输入框，沿用卡片的评论 / 回复事件。 */
function Thread({ id, onBack }: { id: string; onBack: () => void }) {
  const { notify, demo } = useApp();
  const cache = useQueryClient();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const detail = useQuery({
    queryKey: ["card", id],
    queryFn: () => boardApi.card(id),
    refetchInterval: 2000,
  });
  const card = detail.data;
  // 只显示你发的消息和 Bot 的回答；其它评论不混进对话。
  const messages = (card?.events ?? []).filter(
    (e) => (e.kind === "comment" && e.actor === "user") || e.kind === "reply",
  );
  const error =
    card?.status === "FAILED" && typeof card.result?.error === "string"
      ? card.result.error
      : "";
  const working = card?.status === "RUNNING" || card?.status === "WAITING";
  async function send() {
    setBusy(true);
    try {
      await boardApi.sendChat(id, text);
      setText("");
      await cache.invalidateQueries({ queryKey: ["card", id] });
    } catch (e) {
      notify(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="bot-chat">
      <button className="link-button" onClick={onBack}>
        ← 全部对话
      </button>
      <ErrorNotice error={detail.error} />
      <ol className="chat-messages" aria-label="对话">
        {messages.map((e) => (
          <li
            key={e.id}
            className={e.kind === "reply" ? "from-bot" : "from-me"}
          >
            <small>
              {e.kind === "reply" ? `@${card?.assignee_name ?? "Bot"}` : "你"} ·{" "}
              {time(e.created_at)}
            </small>
            <p>{e.body}</p>
          </li>
        ))}
      </ol>
      {card && (
        <p className="muted chat-status" role="status">
          {statusText[card.status]}
          {chatStatusText(card.status)
            ? ` · ${chatStatusText(card.status)}`
            : ""}
          {error && ` · ${error}`}
        </p>
      )}
      <form
        className="card-comment"
        onSubmit={(e) => {
          e.preventDefault();
          void send();
        }}
      >
        <input
          aria-label="私聊消息"
          placeholder="继续问点什么"
          value={text}
          onChange={(e) => setText(e.target.value)}
        />
        <button disabled={busy || demo || working || !text.trim()}>发送</button>
      </form>
    </div>
  );
}

/** 与某个 Bot 的私聊：线程列表 + 新对话。Bot 只读回答，不会改你的文件。 */
export function BotChat({ bot, onClose }: { bot: Bot; onClose: () => void }) {
  const { notify, demo } = useApp();
  const cache = useQueryClient();
  const [thread, setThread] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const chats = useQuery({
    queryKey: ["chats", bot.id],
    queryFn: () => boardApi.chats(bot.id),
    refetchInterval: 5000,
  });
  async function start() {
    setBusy(true);
    try {
      let card: Card;
      try {
        card = await boardApi.createChat(bot.id, text);
      } catch (e) {
        // 接收方无法强制只读时，必须由你明确同意才按 Bot 自己的权限回答。
        if (
          errorText(e).includes("只读") &&
          confirm(
            `${errorText(e)}\n\n仍要按 ${bot.name} 自己的权限档位回答吗？`,
          )
        ) {
          card = await boardApi.createChat(bot.id, text, true);
        } else {
          throw e;
        }
      }
      setText("");
      await cache.invalidateQueries({ queryKey: ["chats", bot.id] });
      setThread(card.id);
    } catch (e) {
      notify(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal title={`与 @${bot.name} 私聊`} onClose={onClose}>
      {thread ? (
        <Thread id={thread} onBack={() => setThread(null)} />
      ) : (
        <div className="bot-chat">
          <p className="muted">
            Bot 会在自己的环境里只读回答，不会修改文件；同一对话会保留上下文。
          </p>
          <ErrorNotice error={chats.error} />
          <ul className="chat-threads">
            {(chats.data ?? []).map((c) => (
              <li key={c.id}>
                <button className="link-button" onClick={() => setThread(c.id)}>
                  {c.title} · {statusText[c.status]}
                </button>
              </li>
            ))}
          </ul>
          <form
            className="card-comment"
            onSubmit={(e) => {
              e.preventDefault();
              void start();
            }}
          >
            <input
              aria-label="新对话"
              placeholder="开始新对话"
              value={text}
              onChange={(e) => setText(e.target.value)}
            />
            <button disabled={busy || demo || !text.trim()}>发送</button>
          </form>
        </div>
      )}
    </Modal>
  );
}
