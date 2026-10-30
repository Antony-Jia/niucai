import { Map as MapIcon } from "lucide-react";
import {
  availabilityText,
  type Availability,
  type MapBot,
  type WorkMapData,
} from "../lib/work-map";

/**
 * 助手忙闲名册：常驻在任务首页，一眼看到谁在工作、谁待命；点击助手把 @名字 写进输入框。
 * 状态直接来自 projectWorkMap，不在这里推断。
 */
export function BotRoster({
  bots,
  summary,
  selected,
  pickable,
  onPick,
  onOpenMap,
  mapOpen,
}: {
  bots: MapBot[];
  summary: WorkMapData["summary"];
  /** 已经被 @ 的助手名称。 */
  selected: string[];
  /** 可以被指定的助手名称（已启用）；其余只展示状态。 */
  pickable: string[];
  onPick: (name: string) => void;
  onOpenMap: () => void;
  mapOpen: boolean;
}) {
  const parts = (
    [
      ["working", "工作中"],
      ["idle", "待命"],
      ["unavailable", "不可用"],
      ["unknown", "状态未知"],
    ] as [Availability, string][]
  ).filter(([key]) => summary[key] > 0);
  return (
    <section className="panel bot-roster" aria-label="助手状态">
      <div className="section-heading">
        <h2>助手</h2>
        <span className="count">
          {bots.length === 0
            ? "还没有助手"
            : parts
                .map(([key, label]) => `${summary[key]} ${label}`)
                .join(" · ")}
        </span>
        <button
          className="roster-map"
          aria-pressed={mapOpen}
          onClick={onOpenMap}
        >
          <MapIcon size={14} />
          {mapOpen ? "正在查看工作地图" : "打开工作地图"}
        </button>
      </div>
      {bots.length === 0 ? (
        <p className="quiet-note">在设置里添加助手后，这里会显示他们的忙闲。</p>
      ) : (
        <ul className="roster-list">
          {bots.map((bot) => (
            <li key={bot.id}>
              <button
                className={`roster-chip ${bot.availability}${
                  selected.includes(bot.name) ? " picked" : ""
                }`}
                aria-pressed={selected.includes(bot.name)}
                disabled={!pickable.includes(bot.name)}
                aria-label={`@${bot.name}，${availabilityText[bot.availability]}`}
                title={`${bot.reason}${bot.nodeName ? ` · ${bot.nodeName}` : ""}（点击在任务里 @TA）`}
                onClick={() => onPick(bot.name)}
              >
                <i aria-hidden="true" />
                <strong>@{bot.name}</strong>
                <small>
                  {availabilityText[bot.availability]}
                  {bot.runningCount > 1 ? ` ×${bot.runningCount}` : ""}
                </small>
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
