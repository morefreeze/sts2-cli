'use strict';

const fs = require('fs');
const { generateActMap } = require('./index.js');
const { PointTypeName } = require('./map_point.js');

function pointId(point) {
  return `${point.coord.col}:${point.coord.row}`;
}

function serializeAlignment(alignment) {
  return {
    ok: alignment.ok === true,
    ambiguous: alignment.ambiguous === true,
    reason: alignment.ok ? null : String(alignment.reason || 'path alignment failed'),
    path_node_ids: alignment.ok ? alignment.path.map(pointId) : [],
  };
}

function serializeGraph(graph, alignment) {
  const points = new Map();
  const add = (point) => {
    if (!point) return;
    points.set(pointId(point), point);
  };
  add(graph.startingPoint);
  add(graph.bossPoint);
  add(graph.secondBossPoint);
  for (const column of graph.grid) for (const point of column) add(point);

  const pathIds = new Map();
  if (alignment.ok) {
    alignment.path.forEach((point, index) => {
      pathIds.set(pointId(point), index);
    });
  }

  const nodes = [...points.entries()]
    .map(([id, point]) => ({
      id,
      col: point.coord.col,
      row: point.coord.row,
      room_type: PointTypeName[point.PointType],
      visited: pathIds.has(id),
      path_index: pathIds.get(id) ?? null,
    }))
    .sort((a, b) => a.row - b.row || a.col - b.col);

  const edges = [];
  for (const [from, point] of points) {
    for (const child of point.Children) {
      edges.push({ from, to: pointId(child) });
    }
  }
  edges.sort((a, b) => a.from.localeCompare(b.from) || a.to.localeCompare(b.to));
  return { nodes, edges };
}

function requireRequestObject(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('request must be a JSON object');
  }
  return value;
}

function generatePayload(value) {
  const request = requireRequestObject(value);
  const { graph, alignment } = generateActMap({
    actId: request.act_id,
    actIndex: request.act_index,
    seedString: request.seed,
    ascension: request.ascension,
    modifiers: request.modifiers,
    isMultiplayer: request.is_multiplayer,
    visited: request.visited,
    allowPartialPath: request.allow_partial_path,
  });
  const serialized = serializeGraph(graph, alignment);
  return {
    schema_version: 1,
    ok: true,
    nodes: serialized.nodes,
    edges: serialized.edges,
    alignment: serializeAlignment(alignment),
  };
}

function canonicalize(value) {
  if (Array.isArray(value)) return value.map(canonicalize);
  if (value !== null && typeof value === 'object') {
    const result = {};
    for (const key of Object.keys(value).sort()) result[key] = canonicalize(value[key]);
    return result;
  }
  return value;
}

function writeJson(value) {
  process.stdout.write(`${JSON.stringify(canonicalize(value))}\n`);
}

function main() {
  try {
    const request = JSON.parse(fs.readFileSync(0, 'utf8'));
    writeJson(generatePayload(request));
  } catch (error) {
    writeJson({
      schema_version: 1,
      ok: false,
      error: error instanceof Error ? error.message : String(error),
    });
    process.exitCode = 1;
  }
}

if (require.main === module) main();

module.exports = { generatePayload, serializeAlignment, serializeGraph };
