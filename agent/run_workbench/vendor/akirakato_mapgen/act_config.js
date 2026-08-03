'use strict';
/**
 * Per-act constants pulled directly from decompiled C#:
 *   Overgrowth.cs / Underdocks.cs / Hive.cs / Glory.cs
 *   MapPointTypeCounts.cs
 *
 * `getMapPointTypes(rng)` mirrors each act's override and IS RNG-consuming.
 * Order matters: NumOfRests is rolled first, then NumOfUnknowns. Don't
 * swap them or every downstream call drifts.
 *
 * Ascension flags (cumulative — ascension N enables levels 1..N):
 *   SwarmingElites = level 1   → bumps NumOfElites from 5 to 8
 *   DoubleBoss     = level 10  → enables hasSecondBoss (Act 3 only? confirm in code)
 */

const MapKey = Object.freeze({
  OVERGROWTH: 'ACT.OVERGROWTH',
  UNDERDOCKS: 'ACT.UNDERDOCKS',
  HIVE:       'ACT.HIVE',
  GLORY:      'ACT.GLORY',
});

// Standard unknown count formula shared by most acts.
function standardRandomUnknownCount(rng) {
  return rng.nextGaussianInt(12, 1, 10, 14);
}

const ACT_CONFIGS = {
  [MapKey.OVERGROWTH]: {
    baseNumberOfRooms: 15,
    getMapPointTypes(rng) {
      const numOfRests    = rng.nextGaussianInt(7, 1, 6, 7);
      const numOfUnknowns = standardRandomUnknownCount(rng);
      return { numOfRests, numOfUnknowns };
    },
  },
  [MapKey.UNDERDOCKS]: {
    baseNumberOfRooms: 15,
    getMapPointTypes(rng) {
      const numOfRests    = rng.nextGaussianInt(7, 1, 6, 7);
      const numOfUnknowns = standardRandomUnknownCount(rng);
      return { numOfRests, numOfUnknowns };
    },
  },
  [MapKey.HIVE]: {
    baseNumberOfRooms: 14,
    getMapPointTypes(rng) {
      const numOfRests    = rng.nextGaussianInt(6, 1, 6, 7);
      const numOfUnknowns = standardRandomUnknownCount(rng) - 1;
      return { numOfRests, numOfUnknowns };
    },
  },
  [MapKey.GLORY]: {
    baseNumberOfRooms: 13,
    getMapPointTypes(rng) {
      const numOfRests    = rng.nextInt(5, 7);
      const numOfUnknowns = standardRandomUnknownCount(rng) - 1;
      return { numOfRests, numOfUnknowns };
    },
  },
};

function getActConfig(actId) {
  const cfg = ACT_CONFIGS[actId];
  if (!cfg) throw new Error(`Unknown act: ${actId}`);
  return cfg;
}

// AscensionLevel enum order in the decompiled source:
//   None=0, SwarmingElites=1, WearyTraveler=2, ..., DoubleBoss=10
// `ascension` in the .run file is a numeric level; flags are cumulative.
function ascensionFlags(level) {
  const lv = level | 0;
  return {
    swarmingElites: lv >= 1,
    doubleBoss:     lv >= 10,
  };
}

// Default elite count: round(5 * (swarmingElites ? 1.6 : 1.0)).
// Source: MapPointTypeCounts.cs:24 (field initializer).
function defaultNumOfElites(swarming) {
  return Math.round(5 * (swarming ? 1.6 : 1.0));
}

module.exports = { MapKey, getActConfig, ascensionFlags, defaultNumOfElites };
