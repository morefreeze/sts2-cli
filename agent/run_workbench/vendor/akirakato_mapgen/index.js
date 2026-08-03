'use strict';
/**
 * Top-level orchestration: from a parsed .run file → per-act SVG strings.
 *
 *   const { generateActMap, renderRun } = require('./mapgen');
 *
 * generateActMap(opts) builds the graph + aligns the user's path, returning
 *   { graph, alignment }.
 *
 * renderRun(runData) does the whole pipeline for all acts in a run and
 *   returns [{ actId, actIndex, svg, alignment }, ...].
 */

const { Rng }                  = require('./rng.js');
const { getDeterministicHashCode } = require('./string_hash.js');
const { buildStandardActMap }  = require('./generator.js');
const { pruneAndRepair }       = require('./pruning.js');
const { alignPath }            = require('./path_align.js');
const { getActConfig, ascensionFlags, defaultNumOfElites } = require('./act_config.js');

function seedToUint32(seedString) {
  return getDeterministicHashCode(seedString) >>> 0;
}

// All boss encounter model_ids from the act's visited history (in visit order).
// Glory @ ascension ≥ 10 has 2 bosses; everywhere else has 0 or 1.
function findBossModelIds(visited) {
  if (!Array.isArray(visited)) return [];
  const out = [];
  for (const entry of visited) {
    if (entry?.map_point_type === 'boss' && entry.rooms?.[0]?.model_id) {
      out.push(entry.rooms[0].model_id);
    }
  }
  return out;
}

// The ancient (start-node) encounter model_id, e.g. "EVENT.NEOW".
function findAncientModelId(visited) {
  if (!Array.isArray(visited)) return null;
  for (const entry of visited) {
    if (entry?.map_point_type === 'ancient' && entry.rooms?.[0]?.model_id) {
      return entry.rooms[0].model_id;
    }
  }
  return null;
}

/**
 * Generate the map for a single act and align the visited path onto it.
 *
 *   actId        — e.g. "ACT.OVERGROWTH" (string, from runData.acts[actIndex])
 *   actIndex     — 0-based index in runData.acts (drives the per-act sub-RNG name)
 *   seedString   — runData.seed
 *   ascension    — runData.ascension (numeric level)
 *   modifiers    — runData.modifiers (array of strings; checked for BIG_GAME_HUNTER)
 *   isMultiplayer
 *   visited      — runData.map_point_history[actIndex] (the per-node array)
 */
function generateActMap({ actId, actIndex, seedString, ascension, modifiers, isMultiplayer, visited, allowPartialPath }) {
  const cfg     = getActConfig(actId);
  const seedU32 = seedToUint32(seedString);

  const flags     = ascensionFlags(ascension || 0);
  const replaceTreasureWithElites = (modifiers || []).some(m => /BIG.*GAME.*HUNTER|BigGameHunter/i.test(String(m)));
  const numOfElites = defaultNumOfElites(flags.swarmingElites);
  // Second boss only spawns on Glory at ascension ≥ 10.
  const hasSecondBoss = (actId === 'ACT.GLORY') && flags.doubleBoss;

  // C# derives the per-act map RNG name as `act_<N+1>_map`.
  const rng = new Rng(seedU32, `act_${actIndex + 1}_map`);

  const graph = buildStandardActMap({
    cfg, rng,
    isMultiplayer: !!isMultiplayer,
    hasSecondBoss,
    replaceTreasureWithElites,
    numOfElites,
  });
  pruneAndRepair(graph);

  const alignment = visited
    ? alignPath(graph, visited, { allowPartial: !!allowPartialPath })
    : { ok: false, reason: 'no visited history' };
  return { graph, alignment };
}

module.exports = { generateActMap, seedToUint32 };
