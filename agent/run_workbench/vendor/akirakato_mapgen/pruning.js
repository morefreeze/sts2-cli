'use strict';
/**
 * Port of MegaCrit.Sts2.Core.Map.MapPathPruning.
 *
 * Up to 3 cycles of:
 *   1. Find duplicate path segments (same coord+type sequences appearing in
 *      multiple root-to-boss paths) and prune all but one.
 *   2. If pruning dropped a room-type count below target, repair by
 *      converting spare Monster nodes back to the missing type.
 *
 * This is the gnarly part of the algorithm — the segment matching uses
 * stringified keys and a pile of edge-case rules. We mirror the C# closely
 * rather than refactoring; the goal is byte-exactness, not elegance.
 */

const { MapPointType } = require('./map_point.js');
const { unstableShuffle, stableShuffle, cmpMapPoint } = require('./shuffles.js');
const { getAllMapPoints, isValidPointType } = require('./generator.js');

function pruneAndRepair(graph) {
  const { grid, startMapPoints, startingPoint, mapLength, pointTypeCounts: counts, rng } = graph;
  for (let i = 0; i < 3; i++) {
    pruneDuplicateSegments(grid, startMapPoints, startingPoint, rng);
    const repaired = repairPrunedPointTypes(graph, counts, rng);
    if (!repaired) break;
  }
}

function repairPrunedPointTypes(graph, counts, rng) {
  let any = false;
  any = repairPointType(graph, MapPointType.Shop,     counts.numOfShops,    rng) || any;
  any = repairPointType(graph, MapPointType.Elite,    counts.numOfElites,   rng) || any;
  any = repairPointType(graph, MapPointType.RestSite, counts.numOfRests,    rng) || any;
  any = repairPointType(graph, MapPointType.Unknown,  counts.numOfUnknowns, rng) || any;
  return any;
}

function repairPointType(graph, type, target, rng) {
  const all     = getAllMapPoints(graph.grid);
  const current = all.filter(p => p.PointType === type).length;
  let need = target - current;
  if (need <= 0) return false;
  const candidates = all.filter(p => p.PointType === MapPointType.Monster && p.CanBeModified);
  stableShuffle(candidates, rng, cmpMapPoint);
  let any = false;
  for (const p of candidates) {
    if (need === 0) break;
    if (isValidPointType(type, p, graph.mapLength)) {
      p.PointType = type;
      need--;
      any = true;
    }
  }
  return any;
}

function pruneDuplicateSegments(grid, startMapPoints, startingPoint, rng) {
  let iter = 0;
  let dups = findMatchingSegments(startingPoint);
  while (prunePaths(grid, startMapPoints, dups, rng)) {
    iter++;
    if (iter > 50) throw new Error(`Unable to prune matching segments in ${iter} iterations`);
    dups = findMatchingSegments(startingPoint);
  }
}

function findMatchingSegments(startingPoint) {
  const allPaths = findAllPaths(startingPoint);
  const segments = new Map();  // key → MapPoint[][]
  for (const path of allPaths) addSegmentsToDictionary(path, segments);
  // Sort the keys for determinism (C# uses SortedDictionary<string, ...>).
  const sortedKeys = [...segments.keys()].sort();
  const dups = [];
  for (const k of sortedKeys) {
    const list = segments.get(k);
    if (list.length > 1) dups.push(list);
  }
  return dups;
}

function findAllPaths(start) {
  if (start.PointType === MapPointType.Boss) return [[start]];
  const out = [];
  for (const child of start.Children) {
    for (const sub of findAllPaths(child)) {
      out.push([start, ...sub]);
    }
  }
  return out;
}

function isValidSegmentStart(p) { return p.Children.size > 1 || p.coord.row === 0; }
function isValidSegmentEnd(p)   { return p.parents.size  >= 2; }

function generateSegmentKey(segment) {
  const start = segment[0];
  const end   = segment[segment.length - 1];
  // C# special-cases row==0 to omit the start col (since startingPoint sits
  // off-grid). We mirror that exactly so our keys match theirs.
  let prefix;
  if (start.coord.row === 0) {
    prefix = `${start.coord.row}-${end.coord.col},${end.coord.row}-`;
  } else {
    prefix = `${start.coord.col},${start.coord.row}-${end.coord.col},${end.coord.row}-`;
  }
  const types = segment.map(p => p.PointType).join(',');
  return prefix + types;
}

function addSegmentsToDictionary(path, segments) {
  for (let i = 0; i < path.length - 1; i++) {
    if (!isValidSegmentStart(path[i])) continue;
    for (let j = 2; j < path.length - i; j++) {
      const end = path[i + j];
      if (!isValidSegmentEnd(end)) continue;
      const seg = path.slice(i, i + j + 1);
      const key = generateSegmentKey(seg);
      if (!segments.has(key)) {
        segments.set(key, [seg]);
      } else if (!anyOverlapping(segments.get(key), seg)) {
        segments.get(key).push(seg);
      }
    }
  }
}

function anyOverlapping(existing, seg) {
  for (const e of existing) if (overlapping(e, seg)) return true;
  return false;
}
function overlapping(a, b) {
  if (a.length < 3 || b.length < 3) return false;
  for (let i = 1; i <= a.length - 2; i++) if (a[i] === b[i]) return true;
  return false;
}

function prunePaths(grid, startMapPoints, matchingSegments, rng) {
  for (const list of matchingSegments) {
    unstableShuffle(list, rng);
    const pruned = pruneAllButLast(grid, startMapPoints, list);
    if (pruned !== 0) return true;
    if (breakAParentChildRelationshipInAnySegment(list)) return true;
  }
  return false;
}

function pruneAllButLast(grid, startMapPoints, matches) {
  let n = 0;
  for (const seg of matches) {
    if (n === matches.length - 1) return n;
    if (pruneSegment(grid, startMapPoints, seg)) n++;
  }
  return n;
}

function isInMap(grid, p) {
  return grid[p.coord.col][p.coord.row] != null
      || p.PointType === MapPointType.Ancient
      || p.PointType === MapPointType.Boss;
}
function isRemoved(grid, p) { return grid[p.coord.col][p.coord.row] == null; }

function pruneSegment(grid, startMapPoints, segment) {
  let any = false;
  for (let i = 0; i < segment.length - 1; i++) {
    const p = segment[i];
    if (!isInMap(grid, p)) return true;  // C# returns true here; matches behaviour.
    if (p.Children.size > 1 || p.parents.size > 1) continue;

    let parentBlock = false;
    for (const par of p.parents) {
      if (par.Children.size === 1 && !isRemoved(grid, par)) { parentBlock = true; break; }
    }
    if (parentBlock) continue;

    const tail = segment.slice(i);
    let tailBlock = false;
    for (const n of tail) {
      if (n.Children.size > 1 && n.parents.size === 1) { tailBlock = true; break; }
    }
    if (tailBlock) continue;

    const last = segment[segment.length - 1];
    if (last.parents.size === 1) return false;

    let blocked = false;
    for (const c of p.Children) {
      if (segment.includes(c)) continue;
      if (c.parents.size === 1) { blocked = true; break; }
    }
    if (blocked) continue;

    removePoint(grid, startMapPoints, p);
    any = true;
  }
  return any;
}

function removePoint(grid, startMapPoints, p) {
  grid[p.coord.col][p.coord.row] = null;
  startMapPoints.delete(p);
  for (const c of [...p.Children]) p.removeChildPoint(c);
  for (const par of [...p.parents]) par.removeChildPoint(p);
}

function breakAParentChildRelationshipInAnySegment(matches) {
  for (const seg of matches) if (breakAParentChildRelationshipInSegment(seg)) return true;
  return false;
}
function breakAParentChildRelationshipInSegment(segment) {
  let any = false;
  for (let i = 0; i < segment.length - 1; i++) {
    const a = segment[i];
    if (a.Children.size < 2) continue;
    const b = segment[i + 1];
    if (b.parents.size === 1) continue;
    a.removeChildPoint(b);
    any = true;
  }
  return any;
}

module.exports = { pruneAndRepair };
