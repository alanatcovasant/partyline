import type { Claim } from "../types";

/** Longest directory prefix shared by every path, cut at a path boundary. */
function commonPathPrefix(paths: string[]): string {
  if (paths.length < 2) return "";
  const shared = paths.reduce((a, b) => {
    let i = 0;
    while (i < a.length && i < b.length && a[i] === b[i]) i++;
    return a.slice(0, i);
  });
  // Cut back to the last '/' so we never elide a partial directory name.
  return shared.slice(0, shared.lastIndexOf("/") + 1);
}

/**
 * The fleet's territory map: who owns what, right now.
 *
 * Claims are absolute paths, so a flat list is a wall of near-identical prefixes. Group by
 * holder — the question this panel answers is "whose is this?" — and elide the prefix every
 * claim shares. Work items (issue:/pr:) are shown separately: they're claims on work, not
 * territory, and have no prefix in common with paths.
 */
export function ClaimTerritory({ claims }: { claims: Claim[] }) {
  if (!claims.length) {
    return <ul className="pills"><li className="muted">Nothing claimed.</li></ul>;
  }
  const isPath = (p: string) => p.startsWith("/") || /^[a-z]:\//i.test(p);
  const common = commonPathPrefix(claims.map((c) => c.resource_path).filter(isPath));

  const byHolder = new Map<string, Claim[]>();
  for (const c of claims) {
    const held = byHolder.get(c.agent_id) ?? [];
    held.push(c);
    byHolder.set(c.agent_id, held);
  }

  return (
    <>
      {common && <p className="muted">under <code>{common}</code></p>}
      {[...byHolder.entries()].map(([agent, held]) => (
        <div key={agent} className="claim-holder">
          <b>{agent}</b>
          <ul className="pills">
            {held.map((c) => {
              const path = c.resource_path;
              const shown = common && isPath(path) && path.startsWith(common)
                ? path.slice(common.length)
                : path;
              return (
                <li key={c.id} title={c.reason ? `${path} — ${c.reason}` : path}>
                  <code>{shown || "."}</code>
                  <time>→ {new Date(c.expires_at).toLocaleTimeString()}</time>
                </li>
              );
            })}
          </ul>
        </div>
      ))}
    </>
  );
}
