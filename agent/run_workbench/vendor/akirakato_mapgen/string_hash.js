'use strict';
/**
 * Port of MegaCrit.Sts2.Core.Helpers.StringHelper.GetDeterministicHashCode.
 *
 * Two parallel FNV-style accumulators over even/odd char positions, combined
 * with hash1 + hash2 * 1566083941. This is the canonical legacy non-randomized
 * .NET string hash (also used by Roslyn / EF for stable hashes); it is NOT
 * the same as `string.GetHashCode()`, which is randomized per-process since
 * .NET Core 2.x. The game uses the deterministic variant because the result
 * has to be stable across runs and machines.
 *
 * 32-bit signed semantics: every multiplication uses Math.imul and every
 * intermediate result is forced through `| 0`. Returns a signed int32.
 */

function getDeterministicHashCode(str) {
  let hash1 = 352654597;
  let hash2 = hash1;

  for (let i = 0; i < str.length; i += 2) {
    // hash1 = ((hash1 << 5) + hash1) ^ c   == hash1 * 33 ^ c
    hash1 = ((Math.imul(hash1, 33)) ^ str.charCodeAt(i)) | 0;
    if (i === str.length - 1) break;
    hash2 = ((Math.imul(hash2, 33)) ^ str.charCodeAt(i + 1)) | 0;
  }

  return ((hash1 + Math.imul(hash2, 1566083941)) | 0);
}

module.exports = { getDeterministicHashCode };
