'use strict';
/**
 * Port of MegaCrit.Sts2.Core.Map.StandardActMap.
 *
 * Pipeline (mirrors the C# constructor exactly):
 *   1. cfg.getMapPointTypes(rng)             — consumes RNG (counts only, then…)
 *   2. generateMap()                          — 7 paths from row 1 to bottom
 *   3. assignPointTypes()                     — fixed rows + queued random fill
 *   4. (optional) MapPathPruning.pruneAndRepair  — done by caller
 *
 * Produced graph shape:
 *   { grid, mapLength, gridCols, startingPoint, bossPoint, secondBossPoint?,
 *     startMapPoints (Set of row-1 entries),
 *     pointTypeCounts: { numOfRests, numOfShops, numOfElites, numOfUnknowns } }
 *
 * The grid is `MapPoint[col][row]` with col in [0,7) and row in [0, mapLength).
 * Row 0 is unused (start point sits below it); paths populate rows 1..mapLength-1.
 */

const { MapPoint, MapPointType } = require('./map_point.js');
const { stableShuffle, cmpInt, cmpMapPoint } = require('./shuffles.js');

const COLS = 7;

const LOWER_RESTRICT  = new Set([MapPointType.RestSite, MapPointType.Elite]);
const UPPER_RESTRICT  = new Set([MapPointType.RestSite]);
const PARENT_RESTRICT = new Set([MapPointType.Elite, MapPointType.RestSite, MapPointType.Treasure, MapPointType.Shop]);
const CHILD_RESTRICT  = PARENT_RESTRICT;
const SIBLING_RESTRICT = new Set([
  MapPointType.RestSite, MapPointType.Monster, MapPointType.Unknown,
  MapPointType.Elite,    MapPointType.Shop,
]);

function makeGrid(cols, rows) {
  const g = new Array(cols);
  for (let c = 0; c < cols; c++) g[c] = new Array(rows).fill(null);
  return g;
}

function getOrCreate(grid, col, row) {
  let p = grid[col][row];
  if (p) return p;
  p = new MapPoint(col, row);
  grid[col][row] = p;
  return p;
}

function generateNextCoord(grid, current, rng) {
  const col   = current.coord.col;
  const left  = Math.max(0, col - 1);
  const right = Math.min(col + 1, COLS - 1);

  // [-1, 0, 1] stable-shuffled (already sorted, so == unstable for this list).
  const choices = stableShuffle([-1, 0, 1], rng, cmpInt);
  for (const choice of choices) {
    let nextCol;
    if      (choice === -1) nextCol = left;
    else if (choice ===  0) nextCol = col;
    else                    nextCol = right;
    if (!hasInvalidCrossover(grid, current, nextCol)) {
      return { col: nextCol, row: current.coord.row + 1 };
    }
  }
  throw new Error(`Cannot find next node — algorithm violated invariant at ${current}`);
}

function hasInvalidCrossover(grid, current, targetX) {
  const dx = targetX - current.coord.col;
  if (dx === 0 || dx === 7) return false;
  const neighbor = grid[targetX][current.coord.row];
  if (!neighbor) return false;
  for (const child of neighbor.Children) {
    if (child.coord.col - neighbor.coord.col === -dx) return true;
  }
  return false;
}

function pathGenerate(grid, startingPoint, mapLength, rng) {
  let current = startingPoint;
  while (current.coord.row < mapLength - 1) {
    const next = generateNextCoord(grid, current, rng);
    const np   = getOrCreate(grid, next.col, next.row);
    current.addChildPoint(np);
    current = np;
  }
}

function generateMap(grid, mapLength, startMapPoints, rng) {
  for (let i = 0; i < 7; i++) {
    let pt = getOrCreate(grid, rng.nextInt(0, COLS), 1);
    if (i === 1) {
      while (startMapPoints.has(pt)) pt = getOrCreate(grid, rng.nextInt(0, COLS), 1);
    }
    startMapPoints.add(pt);
    pathGenerate(grid, pt, mapLength, rng);
  }
}

function forEachInRow(grid, rowIdx, fn) {
  for (let c = 0; c < grid.length; c++) {
    const p = grid[c][rowIdx];
    if (p) fn(p);
  }
}

// ── Type-assignment validity rules (mirror StandardActMap private methods) ──

function isValidForLower(type, p) {
  return p.coord.row >= 6 || !LOWER_RESTRICT.has(type);
}
function isValidForUpper(type, p, mapLength) {
  return p.coord.row < mapLength - 3 || !UPPER_RESTRICT.has(type);
}
function isValidWithParents(type, p) {
  if (!PARENT_RESTRICT.has(type)) return true;
  for (const x of p.parents)  if (x.PointType === type) return false;
  for (const x of p.Children) if (x.PointType === type) return false;
  return true;
}
function isValidWithChildren(type, p) {
  if (!CHILD_RESTRICT.has(type)) return true;
  for (const x of p.Children) if (x.PointType === type) return false;
  return true;
}
function isValidWithSiblings(type, p) {
  if (!SIBLING_RESTRICT.has(type)) return true;
  for (const parent of p.parents) {
    for (const sib of parent.Children) {
      if (sib !== p && sib.PointType === type) return false;
    }
  }
  return true;
}
function isValidPointType(type, p, mapLength) {
  return isValidForUpper(type, p, mapLength)
      && isValidForLower(type, p)
      && isValidWithParents(type, p)
      && isValidWithChildren(type, p)
      && isValidWithSiblings(type, p);
}

function getAllMapPoints(grid) {
  const out = [];
  for (let c = 0; c < grid.length; c++)
    for (let r = 0; r < grid[c].length; r++)
      if (grid[c][r]) out.push(grid[c][r]);
  return out;
}

// Pull the next type from the queue that's valid at this point. Re-enqueues
// rejected entries so they get another chance at a different point. Returns
// MapPointType.Unassigned if nothing in the queue fits.
function getNextValidPointType(queue, p, counts, mapLength) {
  const startLen = queue.length;
  for (let i = 0; i < startLen; i++) {
    const type = queue.shift();
    if (counts.ignore && counts.ignore.has(type)) return type;
    if (isValidPointType(type, p, mapLength))     return type;
    queue.push(type);
  }
  return MapPointType.Unassigned;
}

function assignRemainingTypes(grid, queue, counts, mapLength, rng) {
  for (let attempt = 0; attempt < 3 && queue.length > 0; attempt++) {
    const unassigned = getAllMapPoints(grid).filter(p => p.PointType === MapPointType.Unassigned);
    stableShuffle(unassigned, rng, cmpMapPoint);
    for (const p of unassigned) {
      if (queue.length === 0) break;
      p.PointType = getNextValidPointType(queue, p, counts, mapLength);
    }
  }
}

function assignPointTypes(graph, rng) {
  const { grid, mapLength, startingPoint, bossPoint, secondBossPoint, pointTypeCounts: counts, replaceTreasureWithElites } = graph;

  // Last row: all rest sites (locked).
  forEachInRow(grid, mapLength - 1, (p) => { p.PointType = MapPointType.RestSite; p.CanBeModified = false; });
  // Treasure row (row -7 from top): treasure (or elite under BigGameHunter).
  forEachInRow(grid, mapLength - 7, (p) => {
    p.PointType = replaceTreasureWithElites ? MapPointType.Elite : MapPointType.Treasure;
    p.CanBeModified = false;
  });
  // First row: all monsters (locked).
  forEachInRow(grid, 1, (p) => { p.PointType = MapPointType.Monster; p.CanBeModified = false; });

  // Build the queue [N rests, N shops, N elites, N unknowns] and fill the rest.
  const queue = [];
  for (let i = 0; i < counts.numOfRests;    i++) queue.push(MapPointType.RestSite);
  for (let i = 0; i < counts.numOfShops;    i++) queue.push(MapPointType.Shop);
  for (let i = 0; i < counts.numOfElites;   i++) queue.push(MapPointType.Elite);
  for (let i = 0; i < counts.numOfUnknowns; i++) queue.push(MapPointType.Unknown);
  assignRemainingTypes(grid, queue, counts, mapLength, rng);

  // Anything still unassigned → Monster.
  for (const p of getAllMapPoints(grid)) {
    if (p.PointType === MapPointType.Unassigned) p.PointType = MapPointType.Monster;
  }

  bossPoint.PointType        = MapPointType.Boss;
  startingPoint.PointType    = MapPointType.Ancient;
  if (secondBossPoint) secondBossPoint.PointType = MapPointType.Boss;
}

/**
 * Build a StandardActMap. `rng` is the per-act map RNG already constructed
 * by the caller (Rng(runSeed, "act_<N>_map")). `cfg` is from act_config.js.
 *
 * `elites` lets the caller plug in the SwarmingElites-bumped count.
 * `replaceTreasureWithElites` is normally false (only true under BigGameHunter).
 */
function buildStandardActMap({ cfg, rng, isMultiplayer, hasSecondBoss, replaceTreasureWithElites, numOfElites }) {
  const baseRooms = cfg.baseNumberOfRooms - (isMultiplayer ? 1 : 0);
  const mapLength = baseRooms + 1;
  const grid      = makeGrid(COLS, mapLength);

  // 1. Roll point-type counts (consumes 2 RNG calls before path gen runs).
  const counts = cfg.getMapPointTypes(rng);
  counts.numOfElites = numOfElites;
  counts.numOfShops  = 3;

  // Ghost points sitting outside the grid for start/boss connections.
  const startingPoint = new MapPoint(Math.floor(COLS / 2), 0);
  const bossPoint     = new MapPoint(Math.floor(COLS / 2), mapLength);
  let secondBossPoint = null;
  if (hasSecondBoss) secondBossPoint = new MapPoint(Math.floor(COLS / 2), mapLength + 1);

  const startMapPoints = new Set();

  // 2. Generate the 7 paths.
  generateMap(grid, mapLength, startMapPoints, rng);

  // Wire the boss row and start row connections.
  forEachInRow(grid, mapLength - 1, (p) => p.addChildPoint(bossPoint));
  if (secondBossPoint) bossPoint.addChildPoint(secondBossPoint);
  forEachInRow(grid, 1, (p) => startingPoint.addChildPoint(p));

  const graph = {
    grid, mapLength, gridCols: COLS,
    startingPoint, bossPoint, secondBossPoint,
    startMapPoints,
    pointTypeCounts: counts,
    replaceTreasureWithElites: !!replaceTreasureWithElites,
    rng,
  };

  // 3. Assign types.
  assignPointTypes(graph, rng);

  return graph;
}

module.exports = {
  buildStandardActMap,
  getAllMapPoints,
  forEachInRow,
  isValidPointType,
  COLS,
};
