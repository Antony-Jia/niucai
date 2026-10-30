import { useState } from "react";
import { Header } from "../components/ui";
import { NodesPanel } from "../components/NodesPanel";
import { ComputerPage } from "./Computer";

const tabs = [
  ["computers", "可操作电脑"],
  ["environments", "执行环境"],
] as const;

/**
 * 设备：可操作电脑（可见桌面，可接管）与执行环境（助手运行的节点）分组展示。
 * 节点在线不等于拥有可操作桌面，两者不混为一谈。
 */
export function DevicesPage() {
  const [tab, setTab] = useState<(typeof tabs)[number][0]>("computers");
  return (
    <>
      <Header
        eyebrow="DEVICES"
        title="设备"
        description="可操作电脑用于看见和接管桌面；执行环境是助手实际运行任务的地方。"
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
      {tab === "computers" ? <ComputerPage embedded /> : <NodesPanel />}
    </>
  );
}
