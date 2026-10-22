import {
  describeAction,
  describeResult,
  riskLabel,
  type Field,
} from "../lib/describe";

function Fields({ fields }: { fields: Field[] }) {
  if (!fields.length) return null;
  return (
    <dl className="action-fields">
      {fields.map((f, i) => (
        <div key={`${f.label}-${i}`} className={f.long ? "long" : undefined}>
          <dt>{f.label}</dt>
          <dd className={f.code ? "mono" : undefined}>{f.value}</dd>
        </div>
      ))}
    </dl>
  );
}

/** 动作的可读描述：标题、摘要、参数与（可选）结果。 */
export function ActionCard({
  spec,
  result,
  risk,
  showTitle = true,
}: {
  spec: Record<string, unknown>;
  result?: Record<string, unknown> | null;
  risk?: string;
  showTitle?: boolean;
}) {
  const described = describeAction(spec);
  const outcome = describeResult(result);
  return (
    <div className="action-card">
      {showTitle && (
        <div className="action-card-head">
          <strong>{described.title}</strong>
          {risk && <span className="risk">{riskLabel[risk] ?? risk}</span>}
        </div>
      )}
      {described.summary && (
        <p className="action-summary">{described.summary}</p>
      )}
      <Fields fields={described.fields} />
      {outcome.length > 0 && (
        <>
          <h5>结果</h5>
          <Fields fields={outcome} />
        </>
      )}
    </div>
  );
}
