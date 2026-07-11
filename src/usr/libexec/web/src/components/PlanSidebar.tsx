import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

// Count GFM task-list checkboxes in the raw markdown so headers can show a
// `done/total` progress tally. Matches `- [ ]` / `- [x]` (any indent, `*`/`+`
// bullets too, case-insensitive on the x) — the same syntax remark-gfm renders
// as checkbox <li>s below. Deliberately line-based and cheap; the file is small.
const TASK_RE = /^\s*[-*+]\s+\[([ xX])\]\s/;

export function tally(md: string): { done: number; total: number } {
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

// plan.md holds one `## goal` section per in-flight goal (see the tracking
// contract in boot/bootstrap.py). Split on h2 lines so each goal renders as
// its own group with its own tally; everything before the first `##` (a
// leading `# title`, prose, or a whole flat headerless plan) is the preamble.
// Same cheap line-based parsing as tally() — no fence awareness needed for a
// checklist file.
const H2_RE = /^##\s+(.*\S)\s*$/;

export interface PlanSection {
  title: string;
  body: string;
}

export function splitSections(md: string): {
  preamble: string;
  sections: PlanSection[];
} {
  const preamble: string[] = [];
  const sections: PlanSection[] = [];
  let current: { title: string; lines: string[] } | null = null;
  for (const line of md.split("\n")) {
    const m = H2_RE.exec(line);
    if (m) {
      if (current) sections.push({ title: current.title, body: current.lines.join("\n") });
      current = { title: m[1], lines: [] };
    } else if (current) {
      current.lines.push(line);
    } else {
      preamble.push(line);
    }
  }
  if (current) sections.push({ title: current.title, body: current.lines.join("\n") });
  return { preamble: preamble.join("\n").trim(), sections };
}

function Count({ done, total }: { done: number; total: number }) {
  return (
    <span className="plan-count" title="Steps done / total">
      {done}/{total}
    </span>
  );
}

// Right-hand rail that renders the active PAI's live plan.md as a read-only GFM
// checklist (the PAI owns the file — the owner watches, never checks boxes).
// Each `## goal` section renders as its own group with a per-goal tally; a
// fully-ticked section dims until the PAI deletes it. A flat headerless plan
// renders whole, as before. Empty/absent plan ⇒ the caller collapses the rail.
export function PlanSidebar({ plan, pai }: { plan: string; pai: string | null }) {
  const { done, total } = tally(plan);
  const { preamble, sections } = splitSections(plan);
  return (
    <div className="plan-scroll">
      <div className="plan-head">
        <div className="plan-heading">Plan{pai ? ` · ${pai}` : ""}</div>
        {total > 0 && <Count done={done} total={total} />}
      </div>
      <div className="plan-body">
        {preamble && <ReactMarkdown remarkPlugins={[remarkGfm]}>{preamble}</ReactMarkdown>}
        {sections.map((s, i) => {
          const st = tally(s.body);
          const doneAll = st.total > 0 && st.done === st.total;
          return (
            <section key={i} className={`plan-goal${doneAll ? " done" : ""}`}>
              <div className="plan-goal-head">
                <div className="plan-goal-title">{s.title}</div>
                {st.total > 0 && <Count done={st.done} total={st.total} />}
              </div>
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{s.body}</ReactMarkdown>
            </section>
          );
        })}
      </div>
    </div>
  );
}
