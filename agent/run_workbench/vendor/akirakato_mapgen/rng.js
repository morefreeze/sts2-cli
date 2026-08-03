'use strict';
/**
 * Port of MegaCrit.Sts2.Core.Random.Rng — a thin wrapper around
 * .NET's seeded `Random`, with a Counter so saves can replay state by
 * just storing how many calls have happened since construction.
 *
 * Two ctor forms (mirroring C#):
 *   new Rng(seedUint, counter=0)
 *   new Rng(seedUint, name)        // name is hashed and added to seed
 *
 * The "name" form is what derives per-purpose sub-RNGs from the run seed —
 * map gen uses `new Rng(runSeed, "act_<N>_map")`.
 */

const { NetRandom }              = require('./net_random.js');
const { getDeterministicHashCode } = require('./string_hash.js');

// Force a JS number into a uint32 representation matching `(uint)int` in C#.
const toU32 = (n) => (n >>> 0);
// Force into int32 the way `(int)uint` does in C# (preserves bit pattern).
const toI32 = (n) => (n | 0);

class Rng {
  constructor(seed, counterOrName = 0) {
    let actualSeedU32;
    if (typeof counterOrName === 'string') {
      // C#: `new Rng(seed + (uint)GetDeterministicHashCode(name), 0)`
      const nameHash = getDeterministicHashCode(counterOrName);  // int32
      actualSeedU32  = toU32(seed + toU32(nameHash));            // uint addition wraps mod 2^32
      this.Counter   = 0;
    } else {
      actualSeedU32  = toU32(seed);
      this.Counter   = 0;
    }
    this.Seed = actualSeedU32;
    // C#: `new Random((int)seed)` — bit-pattern reinterpret uint→int
    this._random = new NetRandom(toI32(actualSeedU32));
    if (typeof counterOrName === 'number' && counterOrName > 0) {
      this.fastForwardCounter(counterOrName);
    }
  }

  fastForwardCounter(target) {
    if (this.Counter > target) {
      throw new Error(`Cannot fast-forward Rng counter to lower number (cur=${this.Counter}, target=${target})`);
    }
    while (this.Counter < target) {
      this.Counter++;
      this._random.next();
    }
  }

  nextBool() {
    this.Counter++;
    return this._random.nextInt(2) === 0;
  }

  // Two overloads via arity: (max) → [0,max); (min,max) → [min,max).
  nextInt(a, b) {
    this.Counter++;
    if (b === undefined) return this._random.nextInt(a);
    return this._random.nextInt(a, b);
  }

  nextDouble() {
    this.Counter++;
    return this._random.nextDouble();
  }

  // Box-Muller with rejection — used by map-gen to pick rest/unknown counts.
  // Counter increments by 1 per call (matching C#), but the underlying
  // _random advances by 2 per loop iteration; rejections re-loop until the
  // result lands in [min, max].
  nextGaussianInt(mean, stdDev, min, max) {
    this.Counter++;
    let result;
    do {
      const u1 = 1.0 - this._random.nextDouble();
      const u2 = 1.0 - this._random.nextDouble();
      const z  = Math.sqrt(-2.0 * Math.log(u1)) * Math.sin(2 * Math.PI * u2);
      result   = Math.round(mean + stdDev * z);
    } while (result < min || result > max);
    return result;
  }
}

module.exports = { Rng };
