import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { MessageCircle, Pencil, Plus, X } from "lucide-react";

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
// checklist file. Each fragment carries its 0-based line offset into the full
// document so the edit ops below can map a rendered node back to its source
// line (remark positions are 1-based within the fragment ReactMarkdown sees).
const H2_RE = /^##\s+(.*\S)\s*$/;

export interface PlanSection {
  title: string;
  body: string;
  offset: number;
}

export function splitSections(md: string): {
  preamble: string;
  preambleOffset: number;
  sections: PlanSection[];
} {
  const lines = md.split("\n");
  const preLines: string[] = [];
  const sections: PlanSection[] = [];
  let current: { title: string; lines: string[]; offset: number } | null = null;
  for (let i = 0; i < lines.length; i++) {
    const m = H2_RE.exec(lines[i]);
    if (m) {
      if (current)
        sections.push({ title: current.title, body: current.lines.join("\n"), offset: current.offset });
      current = { title: m[1], lines: [], offset: i + 1 };
    } else if (current) {
      current.lines.push(lines[i]);
    } else {
      preLines.push(lines[i]);
    }
  }
  if (current)
    sections.push({ title: current.title, body: current.lines.join("\n"), offset: current.offset });
  // Trim blank edges off the preamble for rendering, but keep the count of
  // dropped leading lines so positions still map back to the full document.
  let start = 0;
  let end = preLines.length;
  while (start < end && !preLines[start].trim()) start++;
  while (end > start && !preLines[end - 1].trim()) end--;
  return { preamble: preLines.slice(start, end).join("\n"), preambleOffset: start, sections };
}

// -- line-surgery edit ops (exported for tests) --
// All take the FULL plan markdown plus a 0-based absolute line index and
// return the edited document, or null when the line isn't a task step (the
// file changed under us — the SSE reconcile will repaint, so a no-op is safe).

export function toggleStep(md: string, line0: number): string | null {
  const lines = md.split("\n");
  const line = lines[line0];
  if (line === undefined) return null;
  const m = TASK_RE.exec(line);
  if (!m) return null;
  // TASK_RE anchors the checkbox right after the bullet, so the first `[` on
  // the line is the box itself — never a later markdown link.
  const idx = line.indexOf("[");
  lines[line0] = line.slice(0, idx + 1) + (m[1] === " " ? "x" : " ") + line.slice(idx + 2);
  return lines.join("\n");
}

export function removeStep(md: string, line0: number): string | null {
  const lines = md.split("\n");
  if (line0 < 0 || line0 >= lines.length || !TASK_RE.test(lines[line0])) return null;
  lines.splice(line0, 1);
  return lines.join("\n");
}

// Append `- [ ] text` at the end of the fragment starting at `offset` with
// `fragLen` lines, skipping back over trailing blanks so the new step joins
// the section's list instead of floating after a blank line.
export function addStep(md: string, offset: number, fragLen: number, text: string): string {
  const lines = md.split("\n");
  let at = Math.min(offset + fragLen, lines.length);
  while (at > offset && !(lines[at - 1] ?? "").trim()) at--;
  lines.splice(at, 0, `- [ ] ${text}`);
  return lines.join("\n");
}

function Count({ done, total }: { done: number; total: number }) {
  return (
    <span className="plan-count" title="Steps done / total">
      {done}/{total}
    </span>
  );
}

// Inline "+ Add step" affordance rendered after a goal's checklist. Click to
// get a one-line input; Enter commits, Escape cancels, blur commits (a tap
// away on mobile shouldn't silently discard a typed step).
function AddStep({ onAdd }: { onAdd: (text: string) => void }) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState("");
  if (!editing) {
    return (
      <button className="plan-add" onClick={() => setEditing(true)}>
        <Plus size={12} /> Add step
      </button>
    );
  }
  const commit = () => {
    const t = text.trim();
    setEditing(false);
    setText("");
    if (t) onAdd(t);
  };
  return (
    <input
      className="plan-add-input"
      autoFocus
      value={text}
      placeholder="New step…"
      onChange={(e) => setText(e.target.value)}
      onKeyDown={(e) => {
        if (e.key === "Enter") commit();
        if (e.key === "Escape") {
          setEditing(false);
          setText("");
        }
      }}
      onBlur={commit}
    />
  );
}

// Component override for task-list <li>s inside one rendered fragment: the
// whole row toggles its checkbox (links still navigate), and a hover-revealed
// "×" removes the step. `offset` maps the fragment-relative remark position
// back to the absolute line in the full plan document.
function stepComponents(plan: string, offset: number, onEdit: (md: string) => void) {
  return {
    li({ node, className, children, ...props }: any) {
      const isTask = typeof className === "string" && className.includes("task-list-item");
      const line = node?.position?.start?.line;
      if (!isTask || typeof line !== "number") {
        return (
          <li className={className} {...props}>
            {children}
          </li>
        );
      }
      const abs = offset + line - 1;
      return (
        <li className={className} {...props}>
          <div
            className="plan-step"
            onClick={(e) => {
              // Nested subtask rows sit inside this div — stop the bubble so a
              // subtask click doesn't also toggle its parent step.
              e.stopPropagation();
              if ((e.target as HTMLElement).closest("a")) return;
              const next = toggleStep(plan, abs);
              if (next !== null) onEdit(next);
            }}
          >
            {children}
          </div>
          <button
            className="plan-step-remove"
            title="Remove step"
            aria-label="Remove step"
            onClick={(e) => {
              e.stopPropagation();
              const next = removeStep(plan, abs);
              if (next !== null) onEdit(next);
            }}
          >
            <X size={12} />
          </button>
        </li>
      );
    },
  };
}

// Right-hand rail that renders the active PAI's live plan.md as a GFM
// checklist. The PAI authors the file; with `onEdit` wired the owner can reach
// into the same surface — tick/untick a step, remove or add one, or rewrite
// the raw markdown — and the edit round-trips through POST /api/plan (empty
// content deletes the file, same as the PAI's own `rm`). `onDiscuss` seeds the
// composer to talk to the PAI about the plan. Each `## goal` section renders
// as its own group with a per-goal tally; a fully-ticked section dims until
// the PAI deletes it. Empty/absent plan ⇒ the caller collapses the rail.
export function PlanSidebar({
  plan,
  pai,
  onEdit,
  onDiscuss,
}: {
  plan: string;
  pai: string | null;
  onEdit?: (md: string) => void;
  onDiscuss?: () => void;
}) {
  const { done, total } = tally(plan);
  const { preamble, preambleOffset, sections } = splitSections(plan);
  // Raw-markdown edit mode: null = checklist view, string = the draft text.
  const [rawDraft, setRawDraft] = useState<string | null>(null);
  const preambleLen = preamble ? preamble.split("\n").length : 0;
  return (
    <div className="plan-scroll">
      <div className="plan-head">
        <div className="plan-heading">Plan{pai ? ` · ${pai}` : ""}</div>
        <div className="plan-head-actions">
          {total > 0 && <Count done={done} total={total} />}
          {onDiscuss && (
            <button
              className="plan-action"
              title="Talk to the PAI about this plan"
              onClick={onDiscuss}
            >
              <MessageCircle size={13} />
            </button>
          )}
          {onEdit && (
            <button
              className={`plan-action${rawDraft !== null ? " active" : ""}`}
              title={rawDraft !== null ? "Close markdown editor" : "Edit plan as markdown"}
              onClick={() => setRawDraft(rawDraft === null ? plan : null)}
            >
              <Pencil size={13} />
            </button>
          )}
        </div>
      </div>
      {rawDraft !== null && onEdit ? (
        <div className="plan-raw">
          <textarea
            className="plan-raw-text"
            value={rawDraft}
            spellCheck={false}
            onChange={(e) => setRawDraft(e.target.value)}
          />
          <div className="plan-raw-actions">
            <button
              className="plan-raw-save"
              onClick={() => {
                onEdit(rawDraft);
                setRawDraft(null);
              }}
            >
              Save
            </button>
            <button onClick={() => setRawDraft(null)}>Cancel</button>
          </div>
        </div>
      ) : (
        <div className="plan-body">
          {preamble && (
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              components={onEdit ? stepComponents(plan, preambleOffset, onEdit) : undefined}
            >
              {preamble}
            </ReactMarkdown>
          )}
          {onEdit && sections.length === 0 && (
            <AddStep onAdd={(t) => onEdit(addStep(plan, preambleOffset, preambleLen, t))} />
          )}
          {sections.map((s, i) => {
            const st = tally(s.body);
            const doneAll = st.total > 0 && st.done === st.total;
            return (
              <section key={i} className={`plan-goal${doneAll ? " done" : ""}`}>
                <div className="plan-goal-head">
                  <div className="plan-goal-title">{s.title}</div>
                  {st.total > 0 && <Count done={st.done} total={st.total} />}
                </div>
                <ReactMarkdown
                  remarkPlugins={[remarkGfm]}
                  components={onEdit ? stepComponents(plan, s.offset, onEdit) : undefined}
                >
                  {s.body}
                </ReactMarkdown>
                {onEdit && (
                  <AddStep
                    onAdd={(t) => onEdit(addStep(plan, s.offset, s.body.split("\n").length, t))}
                  />
                )}
              </section>
            );
          })}
        </div>
      )}
    </div>
  );
}
