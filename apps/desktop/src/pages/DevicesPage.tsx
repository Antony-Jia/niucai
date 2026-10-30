import { useState } from "react";
import { Header } from "../components/ui";
import { NodesPanel } from "../components/NodesPanel";
import { ScreensPanel } from "../components/ScreensPanel";

const tabs = [
  ["screens", "远程画面"],
  ["environments", "执行环境"],
] as const;

/**
 * 设备：远程画面是一个可切换的列表，包含可接管的电脑与节点上的预览画面；
 * 执行环境管理助手实际运行任务的节点。节点在线不等于拥有可操作桌面，
 * 列表里用“可接管 / 仅查看”标签区分。
 */
export function DevicesPage() {
  const [tab, setTab] = useState<(typeof tabs)[number][0]>("screens");
  return (
    <>
      <Header
        eyebrow="DEVICES"
        title="设备"
        description="远程画面用于查看桌面，其中登记的电脑还可以接管；执行环境是助手实际运行任务的地方。"
      />
      <div className="settings-tabs" role="tablist" aria-label="设备分组">
        {tabs.map(([key, label]) => (
          <button
            key={key}
            role="tab"
            aria-selected={tab === key}
            className={tab === key ? "active" : ""}
            onClick={() => setTab(key)}
          >
            {label}
          </button>
        ))}
      </div>
      {tab === "screens" ? <ScreensPanel /> : <NodesPanel />}
    </>
  );
}
