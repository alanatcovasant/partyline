import { useState } from "react";

// Operator action: archive ALL live messages (moved to the archive, still searchable) and
// clear the live feed. Destructive to the live view, so it confirms first; reloads on success.
export function ArchiveAllButton() {
  const [busy, setBusy] = useState(false);
  const run = async () => {
    if (!window.confirm(
      "Archive ALL messages?\n\nEvery live message is moved to the archive and the live feed is cleared. " +
      "History stays searchable (search + conversations), but the live view resets for everyone.",
    )) return;
    setBusy(true);
    try {
      const r = await fetch("/api/messages/archive-all", { method: "POST" });
      if (!r.ok) throw new Error(String(r.status));
      const { archived } = await r.json();
      window.alert(`Archived ${archived} message(s).`);
      window.location.reload();
    } catch (e) {
      window.alert(`Archive failed: ${e}`);
      setBusy(false);
    }
  };
  return (
    <button type="button" className="ghost archive-all" onClick={run} disabled={busy}
      title="Move all live messages to the archive and clear the live feed">
      {busy ? "archiving…" : "🗄 archive all"}
    </button>
  );
}
