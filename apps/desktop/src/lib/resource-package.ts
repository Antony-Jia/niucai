import { invoke, isTauri } from "@tauri-apps/api/core";

export type ResourceType = "local_docker" | "remote_vps" | "unspecified";
export interface ResourceSetup {
  resourceType: ResourceType;
  kernelUrl: string;
  provider: string;
  model: string;
  baseUrl: string;
  connectionMode?: "compose" | "private_network" | "https" | "ssh_reverse";
  managementUrl?: string;
  kernelInstanceId?: string;
}
export interface NodeCredential {
  id: string;
  token: string;
}

export function validateResourceSetup(input: ResourceSetup): void {
  function address(value: string, kernel: boolean) {
    const url = new URL(value);
    const mode = input.connectionMode;
    if (kernel && mode === "compose" && url.hostname !== "api")
      throw new Error("受管网络内使用 http://api:8080");
    if (kernel && mode === "https" && url.protocol !== "https:")
      throw new Error("公网连接必须使用 HTTPS");
    if (
      !["http:", "https:"].includes(url.protocol) ||
      url.username ||
      url.password ||
      url.search ||
      url.hash ||
      /['$\r\n]/.test(value)
    )
      throw new Error("地址必须是无凭据、查询参数或片段的 HTTP/HTTPS URL");
    if (kernel && mode === "ssh_reverse") {
      if (
        url.protocol !== "http:" ||
        url.hostname !== "127.0.0.1" ||
        url.port !== "18080"
      )
        throw new Error(
          "SSH 反向隧道需使用 http://127.0.0.1:18080（转发容器内回环）",
        );
      return;
    }
    if (
      kernel &&
      ["localhost", "127.0.0.1", "[::1]", "0.0.0.0"].includes(url.hostname)
    )
      throw new Error(
        "容器内 localhost 指向节点自身；本机 Kernel 请使用 host.docker.internal 或可达的网络地址",
      );
    if (
      kernel &&
      input.resourceType === "remote_vps" &&
      mode !== "private_network" &&
      url.protocol !== "https:"
    )
      throw new Error("远程 VPS 需要可达的 HTTPS Kernel 地址");
  }
  address(input.kernelUrl, true);
  if (input.baseUrl) address(input.baseUrl, false);
  for (const value of [input.provider, input.model]) {
    if (!/^[a-zA-Z0-9._:/-]+$/.test(value))
      throw new Error(
        "请填写有效的 provider 和 model（字母、数字及 . _ : / -）",
      );
  }
}

export function resourceFiles(
  input: ResourceSetup,
  credential: NodeCredential,
): Record<string, string> {
  validateResourceSetup(input);
  if (
    input.kernelInstanceId &&
    !/^[a-zA-Z0-9-]{1,36}$/.test(input.kernelInstanceId)
  )
    throw new Error("无效 Kernel 实例身份");
  if (
    !/^[a-zA-Z0-9-]+$/.test(credential.id) ||
    !/^[a-zA-Z0-9_-]+$/.test(credential.token)
  )
    throw new Error("无效的节点凭据");
  const files = {
    ".env": `# API Key 仅在本机填写；不要提交或共享此文件。\nNIUCAI_SOURCE_PATH='../..'\nNIUCAI_KERNEL_URL='${input.kernelUrl.replace(/\/$/, "")}'\nNIUCAI_NODE_ID='${credential.id}'\nNIUCAI_NODE_TOKEN='${credential.token}'\nNIUCAI_PI_PROVIDER='${input.provider}'\nNIUCAI_PI_MODEL='${input.model}'\nNIUCAI_PI_BASE_URL='${input.baseUrl}'\nNIUCAI_PI_PROVIDER_KEY=''\nOPENROUTER_API_KEY=''\nOPENAI_API_KEY=''\nANTHROPIC_API_KEY=''\n`,
    "compose.yml": `name: niucai-node-${credential.id}
services:
  runner:
    image: niucai-pi-node:resource-v1
    build:
      context: \${NIUCAI_SOURCE_PATH:-../..}
      dockerfile: services/node-runner/Dockerfile
    env_file: .env
    read_only: true
    tmpfs: [/tmp]
    cap_drop: [ALL]
    cap_add: [SETUID, SETGID, KILL, CHOWN, DAC_OVERRIDE]
    security_opt: [no-new-privileges:true]
    init: true
    mem_limit: 512m
    restart: unless-stopped
    extra_hosts: ["host.docker.internal:host-gateway"]
    volumes: [node-data:/data]
volumes:
  node-data:
`,
    "README.txt": `资源接入：${input.resourceType === "remote_vps" ? "远程 VPS（Linux）" : "本地 Docker（Linux 容器）"}

1. 在节点机器安装 Docker 和 Compose。Windows Docker Desktop 切换到 Linux 容器。
2. 准备与 Kernel 匹配版本的 niucai 源码，将本包解压至源码根目录下 build-cache/node-${credential.id}/。
   若放在其他位置，请将 .env 的 NIUCAI_SOURCE_PATH 改为源码根目录路径（Windows 推荐正斜杠）。
3. 在节点本机编辑 .env，填写模型 API Key；内置 provider 使用对应的 OPENROUTER_API_KEY、OPENAI_API_KEY 或 ANTHROPIC_API_KEY。
   自定义 OpenAI-compatible provider 填写 NIUCAI_PI_BASE_URL（通常含 /v1）和 NIUCAI_PI_PROVIDER_KEY。
4. ${input.resourceType === "remote_vps" ? "在 VPS 终端中" : "在 PowerShell 或终端中"}进入解压目录，运行：
   docker compose -f compose.yml up -d --build
5. 返回资源页面，等待真实心跳显示在线且 Pi 可用；提交一个工作包并人工批准验证模型执行。

连接说明：容器内 localhost 是节点自身。Kernel 位于宿主机时使用 host.docker.internal，
且 Kernel 必须监听容器可访问的接口；同一 Docker 网络可用服务名，否则使用可达的网络地址。
VPS 使用可达的 HTTPS 地址。节点仅主动访问 Kernel，无需公开节点端口。
无法上线时检查：docker compose -f compose.yml logs --tail 50 runner
停机保留数据：docker compose -f compose.yml down
资源页面的停用只停止新任务调度，不关闭容器。
重置凭据后，用新包中的 .env 更新旧配置，再运行 docker compose -f compose.yml up -d --force-recreate。
不要使用 down -v；原任务工作区和会话保存在独立数据卷中。
此包包含节点凭据，应保存在私有位置；API Key 不上传 Kernel，不挂载宿主机目录或 Docker socket。
`,
  };
  files[".env"] +=
    `NIUCAI_KERNEL_INSTANCE_ID='${input.kernelInstanceId ?? ""}'\n`;
  if (input.connectionMode === "compose") {
    files["compose.yml"] =
      files["compose.yml"].replace(
        '    extra_hosts: ["host.docker.internal:host-gateway"]',
        "    networks: [managed]",
      ) +
      "\nnetworks:\n  managed:\n    external: true\n    name: niucai-managed\n";
  }
  if (input.connectionMode === "ssh_reverse") {
    files["README.txt"] = files["README.txt"].replace(
      "VPS 使用可达的 HTTPS 地址。",
      "SSH 模式先按 tunnel.txt 建立私有通道；配置包内回环地址仅适用于 relay 网络命名空间。",
    );
    files["compose.yml"] = files["compose.yml"]
      .replace(
        '    extra_hosts: ["host.docker.internal:host-gateway"]',
        '    network_mode: "service:relay"\n    depends_on: [relay]',
      )
      .replace(
        "volumes:\n  node-data:",
        `  relay:
    build: {context: ., dockerfile: Dockerfile.relay}
    command: ["TCP-LISTEN:18080,bind=127.0.0.1,reuseaddr,fork", "UNIX-CONNECT:/tunnel/kernel.sock"]
    volumes: ["\${NIUCAI_TUNNEL_DIRECTORY:?set private tunnel directory}:/tunnel:ro"]
    read_only: true
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    restart: unless-stopped
volumes:
  node-data:`,
      );
    files[".env"] += "NIUCAI_TUNNEL_DIRECTORY=''\n";
    Object.assign(files, {
      "Dockerfile.relay":
        'FROM alpine:3.22\nRUN apk add --no-cache socat\nENTRYPOINT ["socat"]\n',
      "tunnel.txt":
        "在 VPS 创建私有目录 ~/.niucai-tunnel（chmod 700），将其绝对路径填入 .env 的 NIUCAI_TUNNEL_DIRECTORY。\n在 Windows 使用 kernel-tunnel.ps1 -SshHost <SSH别名> -RemoteSocket <该目录>/kernel.sock。\nSSH 远端使用私有 Unix socket；relay 容器转换为其命名空间内的 127.0.0.1:18080，Runner 共享 relay 网络命名空间。\n无公网监听、无默认 host network、无模型容器宿主机挂载。SSH 必须预先核验 known_hosts 并使用 key agent。\n诊断：docker compose exec relay socat - TCP:127.0.0.1:18080（发送 GET /healthz HTTP/1.0）。\n",
    });
  }
  return files;
}

// Small uncompressed ZIP writer: no extra runtime dependency, identical in Tauri and PWA.
export function resourceZip(
  files: Record<string, string>,
): Uint8Array<ArrayBuffer> {
  const encoder = new TextEncoder();
  const chunks: Uint8Array[] = [];
  const directory: Uint8Array[] = [];
  let offset = 0;
  let directorySize = 0;
  function header(size: number) {
    const bytes = new Uint8Array(size);
    return { bytes, view: new DataView(bytes.buffer) };
  }
  for (const [path, text] of Object.entries(files)) {
    const name = encoder.encode(path),
      data = encoder.encode(text);
    let crc = 0xffffffff;
    for (const byte of data) {
      crc ^= byte;
      for (let bit = 0; bit < 8; bit++)
        crc = (crc >>> 1) ^ (crc & 1 ? 0xedb88320 : 0);
    }
    crc = (crc ^ 0xffffffff) >>> 0;
    const local = header(30),
      central = header(46);
    local.view.setUint32(0, 0x04034b50, true);
    local.view.setUint16(4, 20, true);
    local.view.setUint16(6, 0x800, true);
    local.view.setUint32(14, crc, true);
    local.view.setUint32(18, data.length, true);
    local.view.setUint32(22, data.length, true);
    local.view.setUint16(26, name.length, true);
    central.view.setUint32(0, 0x02014b50, true);
    central.view.setUint16(4, 20, true);
    central.view.setUint16(6, 20, true);
    central.view.setUint16(8, 0x800, true);
    central.view.setUint32(16, crc, true);
    central.view.setUint32(20, data.length, true);
    central.view.setUint32(24, data.length, true);
    central.view.setUint16(28, name.length, true);
    central.view.setUint32(42, offset, true);
    chunks.push(local.bytes, name, data);
    directory.push(central.bytes, name);
    offset += 30 + name.length + data.length;
    directorySize += 46 + name.length;
  }
  const end = header(22);
  end.view.setUint32(0, 0x06054b50, true);
  end.view.setUint16(8, directory.length / 2, true);
  end.view.setUint16(10, directory.length / 2, true);
  end.view.setUint32(12, directorySize, true);
  end.view.setUint32(16, offset, true);
  const result = new Uint8Array(offset + directorySize + 22);
  let position = 0;
  for (const part of [...chunks, ...directory, end.bytes]) {
    result.set(part, position);
    position += part.length;
  }
  return result;
}

export async function downloadResourcePackage(
  input: ResourceSetup,
  credential: NodeCredential,
) {
  const bytes = resourceZip(resourceFiles(input, credential));
  if (isTauri()) {
    return (
      (await invoke<string | null>("save_resource_package", {
        nodeId: credential.id,
        bytes: Array.from(bytes),
      })) !== null
    );
  }
  const url = URL.createObjectURL(
    new Blob([bytes], {
      type: "application/zip",
    }),
  );
  const link = document.createElement("a");
  link.href = url;
  link.download = `niucai-node-${credential.id}.zip`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 60000);
  return true;
}
