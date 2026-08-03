'use strict';
/**
 * Align the user's visited node sequence (from .run file) onto the generated
 * graph. The .run file gives only an ordered list of `map_point_type` values
 * — no coordinates — so we walk the graph from StartingMapPoint, matching
 * each visited type to a child of the current node. When multiple children
 * share the visited type (rare but possible), we DFS to find a path that
 * satisfies the entire remaining sequence.
 *
 * Returns:
 *   { ok: true,  path: MapPoint[], ambiguous: boolean }
 *   { ok: false, reason: string,   triedPath: MapPoint[] }
 *
 * `ambiguous` is true if more than one valid full path existed — we return
 * the first one found but flag it. In practice this almost never happens;
 * the row-by-row movement constraint usually pins the exact path.
 */

const { MapPointType, RunFileTypeToEnum } = require('./map_point.js');

function visitedTypeForEntry(entry) {
  // Prefer the top-level map_point_type (what the map shows), not room_type
  // (what's inside the unknown). For Neow's room the run logs "ancient" at
  // the top level, "event" inside — we want "ancient".
  const t = entry.map_point_type;
  if (t === undefined || t === null) return null;
  // The very first entry of an act may be "event" for Neow rather than
  // "ancient" depending on schema version; fold both into Ancient for the
  // start-row match.
  if (t === 'event' && entry.rooms?.[0]?.model_id === 'EVENT.NEOW') return MapPointType.Ancient;
  const mapped = RunFileTypeToEnum[t];
  return (mapped === undefined) ? null : mapped;
}

/**
 * Walk visited[] over the graph. visited[0] is expected to be the Ancient
 * (start) entry; we anchor at startingPoint and consume children for each
 * subsequent entry.
 *
 * opts.allowPartial — when true, do not require the last visited entry to
 * be a Boss. Used for the act in which a defeat/abandon ended the run, so
 * the partial path can still be aligned and drawn.
 */
function alignPath(graph, visited, opts = {}) {
  const allowPartial = !!opts.allowPartial;
  const types = visited.map(visitedTypeForEntry);

  // Sanity-check the bookends — if these don't match, the run schema and our
  // generator have drifted and downstream alignment is meaningless.
  if (types[0] !== MapPointType.Ancient) {
    return { ok: false, reason: `expected ancient at index 0, got ${visited[0]?.map_point_type}`, triedPath: [] };
  }
  if (!allowPartial && types[types.length - 1] !== MapPointType.Boss) {
    return { ok: false, reason: `expected boss at last index, got ${visited[visited.length-1]?.map_point_type}`, triedPath: [] };
  }

  // DFS from the start, matching child types row-by-row. Counts solutions so
  // we can report ambiguity, but stops at 2 (we only render the first one).
  const path = [graph.startingPoint];
  let solutions = 0;
  let firstSolution = null;

  function recurse(current, idx) {
    if (solutions >= 2) return;
    if (idx === types.length) {
      // All visited entries matched. Last-match was on the boss target.
      solutions++;
      if (!firstSolution) firstSolution = path.slice();
      return;
    }
    const wantedType = types[idx];
    for (const child of current.Children) {
      if (child.PointType !== wantedType) continue;
      path.push(child);
      recurse(child, idx + 1);
      path.pop();
      if (solutions >= 2) return;
    }
  }
  recurse(graph.startingPoint, 1);

  if (solutions === 0) {
    return { ok: false, reason: `no path through generated graph matches visited types (${types.length} entries)`, triedPath: path.slice() };
  }
  return { ok: true, path: firstSolution, ambiguous: solutions > 1 };
}

module.exports = { alignPath, visitedTypeForEntry };
