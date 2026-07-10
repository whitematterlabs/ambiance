import { useCallback, useEffect, useRef, useState } from "react";
import { withTokenParam } from "../auth";

// A PAI-authored dashboard, framed in a hard-sandboxed iframe. The security
// spine (see server._dashboard): `sandbox="allow-scripts"` with NO
// allow-same-origin gives the iframe an opaque origin, so its arbitrary JS can't
// read the console's cookies/localStorage/session; the server's strict CSP
// blocks it from making any network request. The parent is the ONLY data path —
// it subscribes to the dashboard's declared `channels` (live hub state passed in
// as `data`) and postMessages frames into the frame; nothing flows back out.
//
// The frame is keyed by slug at the call site, so switching dashboards remounts
// (fresh iframe, no stale listeners). We push the current channel values once the
// frame loads and again whenever `data` changes — event-driven, no polling.

export function DashboardView({
  slug,
  title,
  channels,
  data,
}: {
  slug: string;
  title: string;
  channels: string[];
  // Live hub state keyed by channel name (e.g. { procs, fleet, drivers,
  // scheduled }). The parent already holds these from the SSE stream.
  data: Record<string, unknown>;
}) {
  const frameRef = useRef<HTMLIFrameElement>(null);
  // The frame's document (and its message listener) exists once it has loaded;
  // pushing before then would postMessage into the blank pre-load document.
  const [loaded, setLoaded] = useState(false);

  const push = useCallback(() => {
    const win = frameRef.current?.contentWindow;
    if (!win) return;
    for (const ch of channels) {
      if (!(ch in data)) continue;
      // targetOrigin must be "*": the sandboxed frame has an opaque origin, so
      // there is no concrete origin to pin. The payload is only hub data the
      // console already renders — nothing secret crosses this boundary.
      win.postMessage({ type: "pai:data", channel: ch, payload: data[ch] }, "*");
    }
  }, [channels, data]);

  // Push the initial snapshot on load, then again on every data change. A fresh
  // load resets `loaded` (the frame is keyed by slug upstream), so re-selecting a
  // dashboard re-seeds it.
  useEffect(() => {
    if (loaded) push();
  }, [loaded, push]);

  return (
    <section className="conversation dashboard-view">
      <iframe
        ref={frameRef}
        className="dashboard-frame"
        title={title}
        sandbox="allow-scripts"
        src={withTokenParam(`/api/dashboards/${encodeURIComponent(slug)}`)}
        onLoad={() => setLoaded(true)}
      />
    </section>
  );
}
