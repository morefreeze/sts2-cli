'use strict';
/**
 * Port of MegaCrit.Sts2.Core.Extensions.ListExtensions:
 *   StableShuffle<T>   — sort the list (T : IComparable<T>), then Fisher-Yates
 *   UnstableShuffle<T> — Fisher-Yates from end down
 *
 * Note "stable" is .NET's terminology — it refers to determinism of input
 * ordering, not to the conventional sense of "preserves original order for
 * equal keys." Calling StableShuffle on a list that arrived in any order
 * yields the same output, because the sort comes first.
 *
 * Both mutate the list in place AND return it (matches C# fluent style).
 */

// `compareFn` is required for stable shuffle — pass the right one for the
// element type (int, MapPoint by coord, etc). UnstableShuffle never sorts.
function stableShuffle(list, rng, compareFn) {
  list.sort(compareFn);
  return unstableShuffle(list, rng);
}

function unstableShuffle(list, rng) {
  for (let i = list.length - 1; i > 0; i--) {
    const j = rng.nextInt(i + 1);
    const tmp = list[i];
    list[i] = list[j];
    list[j] = tmp;
  }
  return list;
}

// Comparator for ints (used for the [-1, 0, 1] shuffle in path generation).
const cmpInt = (a, b) => a - b;

// Comparator for MapPoint by (col, row) — matches MapCoord.CompareTo which
// uses ValueTuple<int,int>.CompareTo (col first, row second).
const cmpMapPoint = (a, b) => (a.coord.col - b.coord.col) || (a.coord.row - b.coord.row);

module.exports = { stableShuffle, unstableShuffle, cmpInt, cmpMapPoint };
