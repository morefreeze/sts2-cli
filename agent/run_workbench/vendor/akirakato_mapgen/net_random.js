'use strict';
/**
 * Byte-exact port of .NET's seeded `System.Random` (the legacy Knuth
 * subtractive generator preserved in .NET Core / .NET 5+ for backwards
 * compat with seeded constructors). STS2 uses `new Random((int)seed)`
 * everywhere, so this is the algorithm we need to mirror.
 *
 * Source of truth: corefx/src/System.Runtime.Extensions/src/System/Random.cs
 * (the pre-Net5 implementation; `.ctor(int Seed)` retains it forever).
 *
 * All arithmetic stays inside int32 range, but JS numbers are 64-bit
 * floats — so we use Math.imul / |0 only where overflow is a real risk.
 * The subtractive step `a - b` with both in [0, MBIG) cannot overflow
 * either way, so plain subtraction is fine.
 */

const MBIG  = 0x7FFFFFFF;     // 2_147_483_647
const MSEED = 161803398;
const MZ    = 0;

class NetRandom {
  constructor(seed) {
    // Match .NET's `Math.Abs(Seed)` with the int.MinValue special case.
    // (Math.abs(-2147483648) overflows in C#; this branch is the fix.)
    const subtract = (seed === -2147483648) ? MBIG : Math.abs(seed | 0);
    let mj = (MSEED - subtract) | 0;
    let mk = 1;

    const seedArray = new Int32Array(56);  // index 0 unused; algorithm uses 1..55
    seedArray[55] = mj;

    for (let i = 1; i < 55; i++) {
      const ii = (21 * i) % 55;
      seedArray[ii] = mk;
      mk = (mj - mk) | 0;
      if (mk < 0) mk = (mk + MBIG) | 0;
      mj = seedArray[ii];
    }

    for (let k = 1; k < 5; k++) {
      for (let i = 1; i < 56; i++) {
        let n = i + 30;
        if (n >= 55) n -= 55;
        seedArray[i] = (seedArray[i] - seedArray[1 + n]) | 0;
        if (seedArray[i] < 0) seedArray[i] = (seedArray[i] + MBIG) | 0;
      }
    }

    this._seedArray = seedArray;
    this._inext     = 0;
    this._inextp    = 21;
  }

  // The actual subtractive step. Returns int in [0, MBIG).
  _internalSample() {
    let locINext  = this._inext + 1;
    let locINextp = this._inextp + 1;
    if (locINext  >= 56) locINext  = 1;
    if (locINextp >= 56) locINextp = 1;

    let retVal = (this._seedArray[locINext] - this._seedArray[locINextp]) | 0;
    if (retVal === MBIG) retVal--;
    if (retVal < 0)      retVal = (retVal + MBIG) | 0;

    this._seedArray[locINext] = retVal;
    this._inext  = locINext;
    this._inextp = locINextp;
    return retVal;
  }

  // Sample(): protected double Sample() => InternalSample() * (1.0/MBIG)
  _sample() { return this._internalSample() * (1.0 / MBIG); }

  // Used by Next(min,max) when range > int.MaxValue (large-range path).
  // We don't hit this in map-gen (all ranges are small), but include it
  // for completeness so the class is a drop-in replacement.
  _getSampleForLargeRange() {
    let result = this._internalSample();
    if ((this._internalSample() % 2) === 0) result = -result;
    let d = result;
    d += (MBIG - 1);
    d /= (2 * (MBIG - 1) - 1);
    return d;
  }

  // public virtual int Next()  → returns [0, int.MaxValue)
  next() { return this._internalSample(); }

  // public virtual int Next(int maxValue)
  // public virtual int Next(int minValue, int maxValue)
  nextInt(a, b) {
    if (b === undefined) {
      // Next(maxValue)
      if (a < 0) throw new RangeError('maxValue must be >= 0');
      return Math.trunc(this._sample() * a);
    }
    if (a > b) throw new RangeError('minValue > maxValue');
    const range = b - a;
    if (range <= MBIG) return Math.trunc(this._sample() * range) + a;
    // Large range — uses double precision via two samples.
    return Math.trunc(this._getSampleForLargeRange() * range) + a;
  }

  // public virtual double NextDouble()  → returns [0.0, 1.0)
  nextDouble() { return this._sample(); }
}

module.exports = { NetRandom };
