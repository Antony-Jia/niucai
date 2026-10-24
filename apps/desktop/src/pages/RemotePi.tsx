import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Header } from "../components/ui";
import { request } from "../lib/api";
import { useApp } from "../lib/state";
import {
  ResourceManager,
  type ResourceNode,
} from "../components/ResourceManager";

type Engine = "pi" | "codex" | "claude";
type Profile = "read" | "workspace" | "full";
const engineNames: Record<Engine, string> = {
  pi: "Pi",
  codex: "Codex",
  claude: "Claude Code",
};
const profileNames: Record<Profile, string> = {
  read: "只读",
  workspace: "工作区读写",
  full: "完全访问",
};

function describeJob(value: Job) {
  const engine = engineNames[value.engine || "pi"] || value.engine;
  const profile = value.config_snapshot?.permission_profile || "workspace";
  return `${engine} · ${profileNames[profile] || profile}`;
}

interface Job {
  id: string;
  prompt: string;
  status: string;
  node_id: string | null;
  engine?: Engine;
  config_snapshot?: { permission_profile?: Profile };
  parent_task_id: string | null;
  result: {
    answer?: string;
    error?: string;
    files?: { path: string; bytes: number; text?: string }[];
  };
}

export function RemotePi() {
  const { connected, demo, notify } = useApp();
  const cache = useQueryClient();
  const enabled = connected && !demo;
  const nodes = useQuery({
    queryKey: ["remote-nodes"],
    queryFn: () => request<ResourceNode[]>("/api/remote/nodes"),
    enabled,
    refetchInterval: 3000,
  });
  const jobs = useQuery({
    queryKey: ["remote-jobs"],
    queryFn: () => request<Job[]>("/api/remote/jobs"),
    enabled,
    refetchInterval: 2000,
  });
  const config = useQuery({
    queryKey: ["remote-config"],
    queryFn: () => request<{ enabled: boolean }>("/api/remote/config"),
    enabled,
    refetchInterval: 3000,
  });
  const executionEnabled = enabled && config.data?.enabled === true;
  const [prompt, setPrompt] = useState("");
  const [node, setNode] = useState("");
  const [engine, setEngine] = useState<Engine>("pi");
  const [profile, setProfile] = useState<Profile>("workspace");
  const [selected, setSelected] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [submissionKey, setSubmissionKey] = useState<string | null>(null);
  const job = jobs.data?.find((value) => value.id === selected);

  async function act(action: () => Promise<unknown>) {
    setBusy(true);
    try {
      await action();
      await cache.invalidateQueries({ queryKey: ["remote-jobs"] });
      await cache.invalidateQueries({ queryKey: ["remote-nodes"] });
    } catch (error) {
      notify(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <Header
        eyebrow="REMOTE PI"
        title="执行资源与远程任务"
        description="Pi 在各节点独立执行，Kernel 管理排队、授权与结果。每节点同时执行一个任务。"
      />
      {!enabled && (
        <p className="panel">
          连接真实 Kernel 后可管理节点；示例模式不启动远程任务。
        </p>
      )}
      {(nodes.isError || jobs.isError) && (
        <p className="panel" role="alert">
          无法获取远程节点数据，请检查 Kernel 连接和版本。
        </p>
      )}
      {enabled && config.data?.enabled === false && (
        <p className="panel" role="alert">
          远程执行未启用。请在 Kernel API 与 Worker 配置
          NIUCAI_REMOTE_PI_ENABLED=true 并重启；节点 Runner 无需主 Kernel 凭据。
        </p>
      )}
      {config.isError && (
        <p role="alert">无法读取资源配置，请检查 Kernel 版本及连接。</p>
      )}
      <div className="settings-grid">
        <ResourceManager
          nodes={nodes.data || []}
          enabled={executionEnabled}
          busy={busy}
          act={act}
        />
        <section className="panel">
          <h2>提交远程 Pi 任务</h2>
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void act(async () => {
                const key = submissionKey || crypto.randomUUID();
                setSubmissionKey(key);
                const value = await request<Job>("/api/remote/jobs", "POST", {
                  prompt,
                  allowed_nodes: node ? [node] : [],
                  engine,
                  permission_profile: profile,
                  idempotency_key: key,
                });
                setSelected(value.id);
                setPrompt("");
                setSubmissionKey(null);
              });
            }}
          >
            <label>
              执行节点
              <select
                value={node}
                onChange={(event) => {
                  setNode(event.target.value);
                  setSubmissionKey(null);
                }}
              >
                <option value="">自动分配空闲节点</option>
                {nodes.data?.map((value) => (
                  <option key={value.id} value={value.id}>
                    {value.name} · {value.status}
                  </option>
                ))}
              </select>
            </label>
            <label>
              执行引擎
              <select
                value={engine}
                onChange={(event) => {
                  setEngine(event.target.value as Engine);
                  setSubmissionKey(null);
                }}
              >
                {(Object.keys(engineNames) as Engine[]).map((value) => (
                  <option key={value} value={value}>
                    {engineNames[value]}
                  </option>
                ))}
              </select>
            </label>
            <label>
              权限档位
              <select
                value={profile}
                onChange={(event) => {
                  setProfile(event.target.value as Profile);
                  setSubmissionKey(null);
                }}
              >
                {(Object.keys(profileNames) as Profile[]).map((value) => (
                  <option key={value} value={value}>
                    {profileNames[value]}
                  </option>
                ))}
              </select>
            </label>
            {profile === "full" && (
              <p role="alert">
                完全访问会关闭引擎自身的权限询问，仍需你手动批准，且节点必须显式启用。
              </p>
            )}
            <label>
              工作要求
              <textarea
                required
                maxLength={16000}
                rows={5}
                value={prompt}
                onChange={(event) => {
                  setPrompt(event.target.value);
                  setSubmissionKey(null);
                }}
                placeholder="说明目标、边界、验证方式与期望产物"
              />
            </label>
            <button
              className="primary-button"
              disabled={!executionEnabled || busy}
            >
              提交并查看授权
            </button>
          </form>
          <p>
            任务授权后，所选引擎（Pi / Codex / Claude Code）
            可在节点容器中按权限档位自主读写文件和运行命令。任务内操作不会逐项经过主
            Kernel 的设备审批。
          </p>
        </section>
      </div>
      <section className="panel">
        <h2>任务队列与结果</h2>
        {jobs.data?.length === 0 && <p>尚无远程任务。</p>}
        {jobs.data?.map((value) => (
          <div key={value.id} style={{ marginBottom: 12 }}>
            <button
              className="text-button"
              onClick={() => setSelected(value.id)}
            >
              {value.prompt.slice(0, 90)}
            </button>{" "}
            · {describeJob(value)} · {value.status} ·{" "}
            {nodes.data?.find((item) => item.id === value.node_id)?.name ||
              "待分配"}
            {value.parent_task_id && (
              <small> · 主任务 {value.parent_task_id}</small>
            )}
          </div>
        ))}
        {job && (
          <div>
            <h3>{job.status}</h3>
            <p>{describeJob(job)}</p>
            <p style={{ whiteSpace: "pre-wrap" }}>{job.prompt}</p>
            {job.status === "WAITING_APPROVAL" && (
              <>
                <p>
                  批准此工作包，允许 {describeJob(job)} 在隔离节点内自主执行。
                </p>
                <button
                  disabled={busy}
                  onClick={() =>
                    void act(() =>
                      request(`/api/remote/jobs/${job.id}/approve`, "POST"),
                    )
                  }
                >
                  批准执行
                </button>
                <button
                  disabled={busy}
                  onClick={() =>
                    void act(() =>
                      request(`/api/remote/jobs/${job.id}/deny`, "POST"),
                    )
                  }
                >
                  拒绝
                </button>
              </>
            )}
            {["QUEUED", "RUNNING", "LOST"].includes(job.status) && (
              <button
                disabled={busy}
                onClick={() =>
                  void act(() =>
                    request(`/api/remote/jobs/${job.id}/cancel`, "POST"),
                  )
                }
              >
                取消任务
              </button>
            )}
            {["FAILED", "CANCELLED"].includes(job.status) && (
              <button
                disabled={busy}
                onClick={() =>
                  void act(() =>
                    request(`/api/remote/jobs/${job.id}/retry`, "POST"),
                  )
                }
              >
                重试并重新授权
              </button>
            )}
            {["LOST", "CANCELLING"].includes(job.status) && (
              <p>等待节点确认进程退出，资源尚未释放。此时不能启动重复执行。</p>
            )}
            <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
              {job.result.answer || job.result.error}
            </pre>
            {job.result.files?.map((file) => (
              <details key={file.path}>
                <summary>
                  {file.path} · {file.bytes} bytes
                </summary>
                <pre style={{ whiteSpace: "pre-wrap" }}>
                  {file.text ??
                    "文件留存在节点工作区，本版仅回传有限文本产物。"}
                </pre>
              </details>
            ))}
          </div>
        )}
      </section>
    </>
  );
}
