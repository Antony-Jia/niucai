import { useState } from "react";
import { Download, FileText, Plus, Bookmark, Search } from "lucide-react";
import { api, downloadArtifact } from "../lib/api";
import type { Memory } from "../lib/types";
import { useApp, useCommand, useData } from "../lib/state";
import {
  ConnectionEmpty,
  Empty,
  ErrorNotice,
  Header,
  Modal,
  time,
} from "../components/ui";
export function Files() {
  const { connected, notify } = useApp();
  const q = useData(["artifacts"], api.artifacts);
  const [search, setSearch] = useState("");
  const download = useCommand((id: string) => downloadArtifact(id));
  return (
    <>
      <Header
        eyebrow="ARTIFACT LIBRARY"
        title="每一次工作，都有留存。"
        description="浏览任务产出的文件；下载通过 Kernel 认证，在本机选择保存位置。"
      />
      {!connected ? (
        <ConnectionEmpty />
      ) : (
        <>
          <div className="toolbar">
            <div className="search">
              <Search size={16} />
              <input
                aria-label="搜索文件"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="搜索文件路径…"
              />
            </div>
          </div>
          <ErrorNotice error={q.error} />
          <section className="panel">
            {q.data
              ?.filter((a) =>
                a.path.toLowerCase().includes(search.toLowerCase()),
              )
              .map((a) => (
                <div className="file-row" key={a.id}>
                  <div className="task-symbol">
                    <FileText size={20} />
                  </div>
                  <div className="file-info">
                    <strong>{a.path}</strong>
                    <small>
                      {a.media_type} · {time(a.created_at)} · 任务{" "}
                      {a.task_id.slice(0, 8)}
                    </small>
                  </div>
                  <button
                    disabled={download.isPending}
                    onClick={() =>
                      download.mutate(a.id, {
                        onSuccess: (path) => {
                          if (path) notify("文件已保存到 " + path);
                        },
                      })
                    }
                  >
                    <Download size={15} />
                    下载
                  </button>
                </div>
              ))}
            {q.data?.length === 0 && (
              <Empty
                title="文件会出现在这里"
                description="Agent 保存的报告、代码和截图会关联到对应任务。"
              />
            )}
          </section>
        </>
      )}
    </>
  );
}
const memoryKinds: Record<string, string> = {
  semantic: "稳定事实",
  episodic: "经历记录",
  card: "卡片记录",
  goal: "Goal 总结",
};

function MemoryEditor({
  memory,
  onClose,
}: {
  memory: Memory;
  onClose: () => void;
}) {
  const [content, setContent] = useState(memory.content);
  const [tags, setTags] = useState(memory.tags.join(", "));
  const save = useCommand(
    () =>
      api.updateMemory(memory.id, {
        content,
        tags: tags
          .split(/[,，]/)
          .map((t) => t.trim())
          .filter(Boolean),
      }),
    "记忆已更新",
  );
  return (
    <Modal title="编辑记忆" onClose={onClose}>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate(undefined, { onSuccess: onClose });
        }}
      >
        <label>
          内容
          <textarea
            rows={8}
            required
            maxLength={16000}
            value={content}
            onChange={(e) => setContent(e.target.value)}
          />
        </label>
        <label>
          标签
          <input value={tags} onChange={(e) => setTags(e.target.value)} />
        </label>
        <button className="primary full-width" disabled={save.isPending}>
          保存
        </button>
      </form>
    </Modal>
  );
}

export function MemoryPage() {
  const { connected } = useApp();
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("");
  const q = useData(["memories", query, filter], () =>
    query || filter ? api.searchMemories(query, filter) : api.memories(),
  );
  const [add, setAdd] = useState(false);
  const [editing, setEditing] = useState<Memory | null>(null);
  const [content, setContent] = useState("");
  const [kind, setKind] = useState("semantic");
  const [tags, setTags] = useState("");
  const create = useCommand(
    () =>
      api.createMemory(
        content,
        kind,
        tags
          .split(/[,，]/)
          .map((t) => t.trim())
          .filter(Boolean),
      ),
    "记忆已保存",
  );
  const remove = useCommand((id: string) => api.deleteMemory(id), "记忆已删除");
  return (
    <>
      <Header
        eyebrow="CONTEXT THAT LASTS"
        title="让重要的事，留在上下文里。"
        description="稳定事实与经历由你记录；卡片结束和 Goal 总结时 Kernel 会自动写一条执行记录，@kernel 规划时按关键词检索。"
      >
        <button
          className="primary"
          disabled={!connected}
          onClick={() => setAdd(true)}
        >
          <Plus size={16} />
          添加记忆
        </button>
      </Header>
      {!connected ? (
        <ConnectionEmpty />
      ) : (
        <>
          <div className="toolbar">
            <div className="search">
              <Search size={16} />
              <input
                aria-label="搜索记忆"
                placeholder="按关键词搜索"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
            </div>
            <select
              aria-label="记忆类型"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
            >
              <option value="">全部类型</option>
              {Object.entries(memoryKinds).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </div>
          <ErrorNotice error={q.error} />
          <div className="memory-grid">
            {q.data?.map((m) => (
              <article className="panel memory-card" key={m.id}>
                <div className="section-heading">
                  <span className="memory-kind">
                    <Bookmark size={16} />
                    {memoryKinds[m.kind] || m.kind}
                  </span>
                  <small>{time(m.created_at)}</small>
                </div>
                <p className="preserve">{m.content}</p>
                <div className="tag-row">
                  {m.tags.map((t) => (
                    <span key={t}>{t}</span>
                  ))}
                </div>
                <div className="button-row">
                  <button onClick={() => setEditing(m)}>编辑</button>
                  <button
                    onClick={() => {
                      if (confirm("删除这条记忆？")) remove.mutate(m.id);
                    }}
                  >
                    删除
                  </button>
                </div>
              </article>
            ))}
          </div>
          {q.data?.length === 0 && (
            <section className="panel">
              <Empty
                title={query || filter ? "没有匹配的记忆" : "还没有记忆"}
                description="添加偏好、稳定事实，或值得记录的经历。"
              />
            </section>
          )}
        </>
      )}
      {editing && (
        <MemoryEditor memory={editing} onClose={() => setEditing(null)} />
      )}
      {add && (
        <Modal title="添加记忆" onClose={() => setAdd(false)}>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              create.mutate(undefined, {
                onSuccess: () => {
                  setAdd(false);
                  setContent("");
                  setTags("");
                },
              });
            }}
          >
            <label>
              记忆类型
              <select value={kind} onChange={(e) => setKind(e.target.value)}>
                <option value="semantic">稳定事实 / 偏好</option>
                <option value="episodic">经历记录</option>
              </select>
            </label>
            <label>
              内容
              <textarea
                rows={5}
                required
                maxLength={16000}
                value={content}
                onChange={(e) => setContent(e.target.value)}
              />
            </label>
            <label>
              标签
              <input
                value={tags}
                onChange={(e) => setTags(e.target.value)}
                placeholder="逗号分隔，例如：研究，偏好"
              />
            </label>
            <button className="primary full-width" disabled={create.isPending}>
              保存记忆
            </button>
          </form>
        </Modal>
      )}
    </>
  );
}
