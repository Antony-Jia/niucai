import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ErrorNotice, time } from "./ui";
import { useApp } from "../lib/state";
import {
  judgeApi,
  verdictText,
  type JudgeRun,
  type Verdict,
} from "../lib/judge";

const VERDICTS: Verdict[] = ["decided", "returned", "held"];

function RunRow({
  run,
  onCorrected,
}: {
  run: JudgeRun;
  onCorrected: () => void;
}) {
  const { notify, demo } = useApp();
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit() {
    setBusy(true);
    try {
      await judgeApi.correct(run.id, text.trim());
      setEditing(false);
      setText("");
      onCorrected();
    } catch (e) {
      notify(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <li className="judge-run">
      <small>
        {time(run.created_at)} · {run.card_title} ·{" "}
        {run.verdict ? verdictText[run.verdict] : "处理中"}
        {run.cited_ids.length > 0 && `（依据记录 ${run.cited_ids.join("、")}）`}
        {run.error && ` · 已降级：${run.error}`}
      </small>
      <p className="preserve">问：{run.question}</p>
      {run.answer && <p className="preserve">裁判代答：{run.answer}</p>}
      {run.explanation && <p className="muted">{run.explanation}</p>}
      {run.correction ? (
        <p className="muted">已纠正为：{run.correction}</p>
      ) : (
        // 转交用户的问题由用户亲自回答，回答已是决策记录，不需要纠正。
        run.verdict &&
        run.verdict !== "held" && (
          <>
            {!editing ? (
              <button disabled={demo} onClick={() => setEditing(true)}>
                判错了
              </button>
            ) : (
              <div className="card-comment">
                <input
                  aria-label="纠正内容"
                  placeholder="正确的回答应该是…"
                  value={text}
                  onChange={(e) => setText(e.target.value)}
                />
                <button
                  className="primary"
                  disabled={busy || !text.trim()}
                  onClick={() => void submit()}
                >
                  提交纠正
                </button>
                <button onClick={() => setEditing(false)}>取消</button>
              </div>
            )}
          </>
        )
      )}
    </li>
  );
}

/** 裁判裁决列表：三类计数、按类过滤、对单条裁决写纠正（纠正会成为排在原记录前面的新记录）。 */
export function JudgePanel({ goalId }: { goalId?: string | null }) {
  const cache = useQueryClient();
  const [verdict, setVerdict] = useState<Verdict | "">("");
  const runs = useQuery({
    queryKey: ["judge-runs", goalId ?? "", verdict],
    queryFn: () => judgeApi.runs(goalId, verdict),
    refetchInterval: 10000,
  });
  const data = runs.data?.counts ? runs.data : undefined;
  return (
    <section className="judge-panel">
      <h3>裁判</h3>
      <ErrorNotice error={runs.error} />
      {data && (
        <>
          <div className="button-row" role="group" aria-label="裁决过滤">
            {VERDICTS.map((v) => (
              <button
                key={v}
                className={verdict === v ? "primary" : ""}
                aria-pressed={verdict === v}
                onClick={() => setVerdict(verdict === v ? "" : v)}
              >
                {verdictText[v]} {data.counts[v] ?? 0}
              </button>
            ))}
            {data.pending > 0 && <small>处理中 {data.pending}</small>}
          </div>
          {data.runs.length === 0 ? (
            <p className="quiet-note">还没有裁决记录。</p>
          ) : (
            <ol className="judge-runs">
              {data.runs.map((run) => (
                <RunRow
                  key={run.id}
                  run={run}
                  onCorrected={() =>
                    void cache.invalidateQueries({ queryKey: ["judge-runs"] })
                  }
                />
              ))}
            </ol>
          )}
        </>
      )}
    </section>
  );
}
