/** The /agents roster is fetched with include_stale=true so no registered agent is ever
 *  silently omitted (an omitted row and a nonexistent agent look identical to a caller).
 *  The panel then splits it: live/idle agents are the "active" list; stale ones render
 *  dimmed behind the "show all" toggle. A row whose status is missing is treated as
 *  active — hiding a row is the exact bug this backs. */
export function splitRoster<T extends { status?: string | null }>(roster: T[]): {
  active: T[];
  stale: T[];
} {
  const active: T[] = [];
  const stale: T[] = [];
  for (const row of roster) {
    if (row.status === "stale") stale.push(row);
    else active.push(row);
  }
  return { active, stale };
}
