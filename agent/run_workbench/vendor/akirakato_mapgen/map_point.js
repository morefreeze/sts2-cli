'use strict';
/**
 * Port of MegaCrit.Sts2.Core.Map primitives:
 *   MapPointType enum (numeric values matter — used in segment keys)
 *   MapCoord       (POJO with col, row)
 *   MapPoint       (graph node with parents, children Sets, PointType, coord)
 *
 * HashSets in C# default to reference equality; we use plain JS Sets which
 * also compare by identity for object refs. ✓
 */

const MapPointType = Object.freeze({
  Unassigned: 0,
  Unknown:    1,
  Shop:       2,
  Treasure:   3,
  RestSite:   4,
  Monster:    5,
  Elite:      6,
  Boss:       7,
  Ancient:    8,
});

const PointTypeName = Object.freeze({
  0: 'Unassigned', 1: 'Unknown', 2: 'Shop',     3: 'Treasure', 4: 'RestSite',
  5: 'Monster',    6: 'Elite',   7: 'Boss',     8: 'Ancient',
});

// Mapping from .run file's lowercase strings to MapPointType values.
const RunFileTypeToEnum = Object.freeze({
  'unknown':   MapPointType.Unknown,
  'shop':      MapPointType.Shop,
  'treasure':  MapPointType.Treasure,
  'rest_site': MapPointType.RestSite,
  'monster':   MapPointType.Monster,
  'elite':     MapPointType.Elite,
  'boss':      MapPointType.Boss,
  'ancient':   MapPointType.Ancient,
  // 'event' shouldn't appear at the map level — events are inside Unknown
  // rooms — but if it does (e.g. Neow's room), treat it as Ancient or Unknown
  // depending on context. The caller should special-case.
});

class MapPoint {
  constructor(col, row) {
    this.coord       = { col, row };
    this.parents     = new Set();
    this.Children    = new Set();
    this.PointType   = MapPointType.Unassigned;
    this.CanBeModified = true;
  }
  addChildPoint(child) {
    this.Children.add(child);
    child.parents.add(this);
  }
  removeChildPoint(child) {
    this.Children.delete(child);
    child.parents.delete(this);
  }
  toString() {
    return `Point[${this.coord.col},${this.coord.row}]`;
  }
}

module.exports = { MapPoint, MapPointType, PointTypeName, RunFileTypeToEnum };
