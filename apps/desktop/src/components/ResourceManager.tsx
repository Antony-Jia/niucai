import { useEffect, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import { native, request } from "../lib/api";
import { useApp } from "../lib/state";
import {
  downloadResourcePackage,
  validateResourceSetup,
  type NodeCredential,
  type ResourceSetup,
  type ResourceType,
} from "../lib/resource-package";
import { Modal, time } from "./ui";

export interface ResourceNode {
  id: string;
  name: string;
  resource_type: ResourceType;
  status: string;
  connection_status: string;
  protocol_status?: string;
  enabled: boolean | number;
  heartbeat_at: string | null;
  capabilities: {
    version?: string;
    available?: boolean;
    node_model?: { provider?: string; model?: string };
  };
  active_job: { id: string; status: string } | null;
}
const names = {
  local_docker: "本地 Docker",
  remote_vps: "远程 VPS",
  unspecified: "未分类",
};
const initial: ResourceSetup = {
  resourceType: "local_docker",
  kernelUrl: "",
  provider: "openrouter",
  model: "",
  baseUrl: "",
};

export function ResourceManager({
  nodes,
  enabled,
  busy,
  act,
}: {
  nodes: ResourceNode[];
  enabled: boolean;
  busy: boolean;
  act: (action: () => Promise<unknown>) => Promise<void>;
}) {
  const { profile } = useApp();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [setup, setSetup] = useState(initial);
  const [credential, setCredential] = useState<NodeCredential | null>(null);
  const [rotating, setRotating] = useState<ResourceNode | null>(null);
  const [editing, setEditing] = useState<ResourceNode | null>(null);
  const [editName, setEditName] = useState("");
  const [downloaded, setDownloaded] = useState(false);
  const [nodeModelKey, setNodeModelKey] = useState("");
  const generation = useRef(0);
  function close() {
    generation.current++;
    setOpen(false);
    setCredential(null);
    setRotating(null);
    setDownloaded(false);
    setNodeModelKey("");
  }
  useEffect(() => {
    generation.current++;
    setOpen(false);
    setCredential(null);
    setRotating(null);
    setDownloaded(false);
    setEditing(null);
    setNodeModelKey("");
    return () => {
      generation.current++;
    };
  }, [enabled, profile?.base_url, profile?.kernel_instance_id]);
  const online = nodes.find((node) => node.id === credential?.id);
  return (
    <section className="panel">
      <h2>执行资源</h2>
      <button
        disabled={!enabled || busy}
        onClick={() => {
          setSetup(initial);
          setName("");
          setRotating(null);
          setCredential(null);
          setOpen(true);
        }}
      >
        新增资源
      </button>
      {nodes.length === 0 && (
        <p>尚无资源。接入本地 Docker 或远程 VPS 后可分配工作包。</p>
      )}
      {nodes.map((node) => (
        <article key={node.id} style={{ marginTop: 18 }}>
          <strong>{node.name}</strong> · {names[node.resource_type] || "未分类"}
          <p>
            <small>{node.id}</small>
          </p>
          <p>
            {(
              { PENDING: "待接入", ONLINE: "在线", OFFLINE: "离线" } as Record<
                string,
                string
              >
            )[node.connection_status] || node.status}{" "}
            · {node.enabled ? "调度已启用" : "调度已停用"} ·{" "}
            {node.capabilities.available ? "Pi 可用" : "Pi 未就绪"} · Pi{" "}
            {node.capabilities.version || "待上报"}
            {` · 协议 ${node.protocol_status ?? "未验证"}`}
            {node.capabilities.node_model?.model &&
              ` · 节点模型 ${node.capabilities.node_model.provider}/${node.capabilities.node_model.model}`}
          </p>
          <p>
            最后心跳：{node.heartbeat_at ? time(node.heartbeat_at) : "尚未连接"}{" "}
            ·{" "}
            {node.active_job
              ? `占用任务 ${node.active_job.id}（${node.active_job.status}）`
              : "空闲"}
          </p>
          <div className="button-row">
            <button
              disabled={!enabled || busy}
              onClick={() => {
                setEditing(node);
                setEditName(node.name);
              }}
            >
              修改资源
            </button>
            <button
              disabled={
                !enabled || busy || (!!node.active_job && !!node.enabled)
              }
              onClick={() =>
                void act(() =>
                  request(`/api/remote/nodes/${node.id}`, "PATCH", {
                    enabled: !node.enabled,
                  }),
                )
              }
            >
              {node.enabled ? "停用调度" : "启用调度"}
            </button>
            <button
              disabled={!enabled || busy || !!node.active_job}
              onClick={() => {
                setRotating(node);
                setSetup({
                  ...initial,
                  resourceType:
                    node.resource_type === "unspecified"
                      ? "local_docker"
                      : node.resource_type,
                });
                setCredential(null);
                setOpen(true);
              }}
            >
              重置接入凭据
            </button>
          </div>
          {node.active_job && (
            <p>停用和重置需等待原任务确认退出；LOST 任务需先由 Runner 核对。</p>
          )}
        </article>
      ))}
      {editing && (
        <Modal title="修改资源" onClose={() => setEditing(null)}>
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void act(async () => {
                await request(`/api/remote/nodes/${editing.id}`, "PATCH", {
                  name: editName,
                  resource_type: editing.resource_type,
                });
                setEditing(null);
              });
            }}
          >
            <label>
              资源名称
              <input
                required
                maxLength={200}
                value={editName}
                onChange={(event) => setEditName(event.target.value)}
              />
            </label>
            <label>
              资源类型
              <select
                value={editing.resource_type}
                onChange={(event) =>
                  setEditing({
                    ...editing,
                    resource_type: event.target.value as ResourceType,
                  })
                }
              >
                {Object.entries(names).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <button disabled={busy || !editName.trim()}>保存资源</button>
          </form>
        </Modal>
      )}
      {open && (
        <Modal
          title={rotating ? "重置资源接入" : "新增执行资源"}
          onClose={close}
        >
          {credential ? (
            <div>
              <p>
                节点凭据仅本次可用，关闭窗口后将清除。下载包包含凭据，请保存在私有位置。
              </p>
              <button
                onClick={() =>
                  void act(async () => {
                    const current = generation.current;
                    const saved = await downloadResourcePackage(
                      setup,
                      credential,
                    );
                    if (current === generation.current) setDownloaded(saved);
                  })
                }
              >
                下载配置包
              </button>
              {downloaded && (
                <p>
                  配置包已生成。解压、在本机 .env 填写模型 API Key，再按
                  README.txt 启动。
                </p>
              )}
              <p>
                {online?.connection_status === "ONLINE"
                  ? "节点已上线。可提交工作包并批准执行，验证模型和结果。"
                  : "等待节点真实心跳上线…"}
              </p>
              <button onClick={close}>关闭并清除凭据</button>
              {native() && setup.connectionMode === "compose" && (
                <>
                  <label>
                    节点模型 API Key
                    <input
                      type="password"
                      autoComplete="off"
                      value={nodeModelKey}
                      onChange={(e) => setNodeModelKey(e.target.value)}
                    />
                  </label>
                  <button
                    disabled={busy || !nodeModelKey}
                    onClick={() =>
                      void act(async () => {
                        const source = localStorage.getItem(
                          "niucai.managed-source",
                        );
                        if (!source)
                          throw new Error(
                            "请先在设置中配置受管 Kernel 源码目录",
                          );
                        await invoke("install_managed_runner", {
                          source,
                          input: {
                            ...credential,
                            modelKey: nodeModelKey,
                            provider: setup.provider,
                            model: setup.model,
                            baseUrl: setup.baseUrl,
                            kernelInstanceId: setup.kernelInstanceId,
                          },
                        });
                        setNodeModelKey("");
                      })
                    }
                  >
                    自动安装到受管网络
                  </button>
                  <p>
                    节点凭据使用 Windows
                    安全存储；模型密钥仅通过本机安装命令传入节点，不上传
                    Kernel。安装完成后仍需真实心跳验证。
                  </p>
                </>
              )}
            </div>
          ) : (
            <form
              onSubmit={(event) => {
                event.preventDefault();
                void act(async () => {
                  validateResourceSetup(setup);
                  const current = generation.current;
                  const identity = await request<{
                    kernel_instance_id: string;
                  }>("/api/kernel/identity");
                  const configured = {
                    ...setup,
                    kernelInstanceId: identity.kernel_instance_id,
                  };
                  if (current === generation.current) setSetup(configured);
                  const value = await request<NodeCredential>(
                    rotating
                      ? `/api/remote/nodes/${rotating.id}/rotate-token`
                      : "/api/remote/nodes",
                    "POST",
                    rotating
                      ? undefined
                      : {
                          name: name.trim(),
                          resource_type: setup.resourceType,
                          ...(setup.connectionMode
                            ? {
                                connection: {
                                  mode: setup.connectionMode,
                                  runner_url: setup.kernelUrl,
                                  management_url: profile?.base_url,
                                  kernel_instance_id:
                                    identity.kernel_instance_id,
                                },
                              }
                            : {}),
                        },
                  );
                  if (current === generation.current)
                    setCredential({ id: value.id, token: value.token });
                });
              }}
            >
              {!rotating && (
                <>
                  <label>
                    资源名称
                    <input
                      required
                      maxLength={200}
                      value={name}
                      onChange={(event) => setName(event.target.value)}
                    />
                  </label>
                  <label>
                    资源类型
                    <select
                      value={setup.resourceType}
                      onChange={(event) =>
                        setSetup({
                          ...setup,
                          resourceType: event.target.value as ResourceType,
                        })
                      }
                    >
                      <option value="local_docker">本地 Docker</option>
                      <option value="remote_vps">远程 VPS</option>
                    </select>
                  </label>
                </>
              )}
              <label>
                连接模式
                <select
                  value={setup.connectionMode ?? ""}
                  onChange={(event) =>
                    setSetup({
                      ...setup,
                      connectionMode: event.target
                        .value as ResourceSetup["connectionMode"],
                      kernelUrl:
                        event.target.value === "compose"
                          ? "http://api:8080"
                          : event.target.value === "ssh_reverse"
                            ? "http://127.0.0.1:18080"
                            : setup.kernelUrl,
                    })
                  }
                >
                  <option value="">手动兼容模式</option>
                  <option value="compose">受管 Compose 网络</option>
                  <option value="https">HTTPS 入口</option>
                  <option value="private_network">已配置私网</option>
                  <option value="ssh_reverse">SSH 反向隧道 + 转发容器</option>
                </select>
              </label>
              <label>
                节点可访问的 Kernel 地址
                <input
                  required
                  type="url"
                  value={setup.kernelUrl}
                  placeholder={
                    setup.resourceType === "remote_vps"
                      ? "https://kernel.example.com"
                      : "http://host.docker.internal:8080"
                  }
                  onChange={(event) =>
                    setSetup({ ...setup, kernelUrl: event.target.value })
                  }
                />
              </label>
              <p>
                {setup.resourceType === "remote_vps"
                  ? "VPS 需使用可达的 HTTPS 地址，只主动连接 Kernel。"
                  : "Windows Docker 使用 Linux 容器。容器内 localhost 指向节点自身；宿主机可用 host.docker.internal，Kernel 必须监听可达接口。"}
              </p>
              <label>
                模型 provider
                <input
                  required
                  value={setup.provider}
                  onChange={(event) =>
                    setSetup({ ...setup, provider: event.target.value })
                  }
                />
              </label>
              <label>
                模型 model
                <input
                  required
                  value={setup.model}
                  placeholder="填写该 provider 支持的模型 ID"
                  onChange={(event) =>
                    setSetup({ ...setup, model: event.target.value })
                  }
                />
              </label>
              <label>
                自定义模型服务地址（可选）
                <input
                  type="url"
                  value={setup.baseUrl}
                  placeholder="https://model.example.com/v1"
                  onChange={(event) =>
                    setSetup({ ...setup, baseUrl: event.target.value })
                  }
                />
              </label>
              <p>模型 API Key 在节点本机 .env 中填写，不上传 Kernel。</p>
              {rotating && (
                <p>提交后旧凭据立即失效；请用新配置更新原容器并保留数据卷。</p>
              )}
              <button
                disabled={!enabled || busy || (!rotating && !name.trim())}
              >
                {rotating ? "重置并生成配置" : "创建资源并生成配置"}
              </button>
            </form>
          )}
        </Modal>
      )}
    </section>
  );
}
