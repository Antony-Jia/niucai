import { configureBrowser } from "../../desktop/src/lib/api";
import type { Profile } from "../../desktop/src/lib/types";
const guest: Profile = {
  base_url: location.origin,
  computer_url: "",
  has_token: false,
  remember_token: false,
  platform: "pwa",
};
async function request<T>(
  path: string,
  method = "GET",
  body?: unknown,
): Promise<T> {
  if (!path.startsWith("/api/") || path.includes(".."))
    throw new Error("无效的 API 地址");
  const response = await fetch(path, {
    method,
    credentials: "same-origin",
    cache: "no-store",
    redirect: "error",
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    if (response.status === 401)
      window.dispatchEvent(new Event("session-expired"));
    let detail: unknown;
    try {
      detail = (await response.json()).detail;
    } catch {}
    throw new Error(
      typeof detail === "string" ? detail : `请求失败（${response.status}）`,
    );
  }
  return response.json() as Promise<T>;
}
configureBrowser({
  request,
  loadProfile: async () => {
    try {
      return {
        ...(await request<Profile>("/api/auth/session")),
        base_url: location.origin,
      };
    } catch {
      return guest;
    }
  },
  connect: async (input) => ({
    ...(await request<Profile>("/api/auth/session", "POST", {
      token: input.token,
    })),
    base_url: location.origin,
  }),
  disconnect: async () => {
    await request("/api/auth/logout", "POST");
  },
  downloadArtifact: async (id) => {
    const metadata = await request<{ path: string }>(`/api/artifacts/${id}`);
    const response = await fetch(`/api/artifacts/${id}/content`, {
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error",
    });
    if (!response.ok) throw new Error("文件下载失败");
    if (Number(response.headers.get("content-length")) > 100 * 1024 * 1024)
      throw new Error("手机客户端下载上限为 100 MB");
    const reader = response.body?.getReader();
    if (!reader) throw new Error("无法读取文件");
    const chunks: Uint8Array<ArrayBuffer>[] = [];
    let size = 0;
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > 100 * 1024 * 1024) {
        await reader.cancel();
        throw new Error("手机客户端下载上限为 100 MB");
      }
      chunks.push(value);
    }
    const url = URL.createObjectURL(
      new Blob(chunks, {
        type:
          response.headers.get("content-type") || "application/octet-stream",
      }),
    );
    const a = document.createElement("a");
    a.href = url;
    a.download = metadata.path.split("/").pop() || "artifact";
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
    return a.download;
  },
});
