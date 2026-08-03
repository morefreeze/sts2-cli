'use strict';

const fs = require('fs');
const { generateActMap } = require('./index.js');
const { PointTypeName } = require('./map_point.js');

const MAX_INPUT_BYTES = 1024 * 1024;
const MAX_VISITED_NODES = 256;
const READ_CHUNK_BYTES = 64 * 1024;

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

function isPlainObject(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function validateRequest(value) {
  if (!isPlainObject(value)) {
    throw new Error('request must be a JSON object');
  }
  if (typeof value.act_id !== 'string' || value.act_id.trim() === '') {
    throw new Error('act_id must be a non-empty string');
  }
  if (!Number.isSafeInteger(value.act_index) || value.act_index < 0 || value.act_index > 3) {
    throw new Error('act_index must be an integer from 0 to 3');
  }
  if (typeof value.seed !== 'string' || value.seed.trim() === '') {
    throw new Error('seed must be a non-empty string');
  }
  if (!Number.isSafeInteger(value.ascension) || value.ascension < 0 || value.ascension > 10) {
    throw new Error('ascension must be an integer from 0 to 10');
  }
  if (!Array.isArray(value.modifiers) || !value.modifiers.every((item) => typeof item === 'string')) {
    throw new Error('modifiers must be an array of strings');
  }
  if (typeof value.is_multiplayer !== 'boolean') {
    throw new Error('is_multiplayer must be a boolean');
  }
  if (value.visited !== null && !Array.isArray(value.visited)) {
    throw new Error('visited must be null or an array of objects');
  }
  if (Array.isArray(value.visited)) {
    if (value.visited.length > MAX_VISITED_NODES) {
      throw new Error(`visited exceeds ${MAX_VISITED_NODES} nodes`);
    }
    if (!value.visited.every(isPlainObject)) {
      throw new Error('visited must be null or an array of objects');
    }
  }
  if (typeof value.allow_partial_path !== 'boolean') {
    throw new Error('allow_partial_path must be a boolean');
  }
  return value;
}

function generatePayload(value) {
  const request = validateRequest(value);
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

function readBoundedInput(fd = 0) {
  const chunks = [];
  let totalBytes = 0;
  while (true) {
    const remainingProbeBytes = (MAX_INPUT_BYTES + 1) - totalBytes;
    const buffer = Buffer.allocUnsafe(Math.min(READ_CHUNK_BYTES, remainingProbeBytes));
    const bytesRead = fs.readSync(fd, buffer, 0, buffer.length, null);
    if (bytesRead === 0) break;
    totalBytes += bytesRead;
    if (totalBytes > MAX_INPUT_BYTES) {
      throw new Error(`input exceeds ${MAX_INPUT_BYTES} bytes`);
    }
    chunks.push(buffer.subarray(0, bytesRead));
  }
  return Buffer.concat(chunks, totalBytes).toString('utf8');
}

function main() {
  try {
    const request = JSON.parse(readBoundedInput());
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

module.exports = {
  MAX_INPUT_BYTES,
  MAX_VISITED_NODES,
  generatePayload,
  readBoundedInput,
  serializeAlignment,
  serializeGraph,
  validateRequest,
};
