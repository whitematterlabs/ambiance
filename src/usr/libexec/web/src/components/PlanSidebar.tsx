import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

// Count GFM task-list checkboxes in the raw markdown so the header can show a
// `done/total` progress tally. Matches `- [ ]` / `- [x]` (any indent, `*`/`+`
// bullets too, case-insensitive on the x) — the same syntax remark-gfm renders
// as checkbox <li>s below. Deliberately line-based and cheap; the file is small.
const TASK_RE = /^\s*[-*+]\s+\[([ xX])\]\s/;

function tally(md: string): { done: number; total: number } {
  let done = 0;
  let total = 0;
  for (const line of md.split("\n")) {
    const m = TASK_RE.exec(line);
    if (!m) continue;
    total += 1;
    if (m[1] !== " ") done += 1;
  }
  return { done, total };
}

// Right-hand rail that renders the active PAI's live plan.md as a read-only GFM
// checklist (the PAI owns the file — the owner watches, never checks boxes). The
// whole file renders; a leading `# title` and any freeform prose come through as
// ordinary markdown. Empty/absent plan ⇒ the caller collapses the rail, so this
// only renders when there's something to show.
export function PlanSidebar({ plan, pai }: { plan: string; pai: string | null }) {
  const { done, total } = tally(plan);
  return (
    <div className="plan-scroll">
      <div className="plan-head">
        <div className="plan-heading">Plan{pai ? ` · ${pai}` : ""}</div>
        {total > 0 && (
          <span className="plan-count" title="Steps done / total">
            {done}/{total}
          </span>
        )}
      </div>
      <div className="plan-body">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{plan}</ReactMarkdown>
      </div>
    </div>
  );
}
