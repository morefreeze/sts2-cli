"""Fool-only drafting heuristics for v0.113.1; no combat policy changes.

Effect counts are separate from game DynamicVar names: `Cards` can mean
Sadness creation, cards inspected, or actual draw. Coefficients below are
conservative heuristics, not a fitted win-probability model. Sources and
validation limits are recorded in docs/fool-a9-strategy.md.
"""
from collections import Counter
import json
from pathlib import Path

METADATA = json.loads((Path(__file__).resolve().parents[1] /
                       'data/fool_card_metadata.json').read_text())['cards']


def card_id(card):
    value = card.get('id', '')
    if isinstance(value, dict):
        value = value.get('en', '')
    return str(value).upper().removeprefix('CARD.')


def is_fool(card):
    return card_id(card) in METADATA


def is_fool_deck(deck):
    deck = deck or []
    ids = [card_id(c) for c in deck]
    return (any(c in {'STRIKE_FOOL', 'DEFEND_FOOL'} for c in ids)
            or sum(is_fool(c) for c in deck) >= max(3, len(deck) / 2))


def _stats(card):
    meta = METADATA[card_id(card)]
    values = dict(meta['stats'])
    if card.get('upgraded'):
        for key, change in meta['upgrade'].items():
            if key in values and isinstance(change, str) and change.startswith(('+', '-')):
                values[key] = meta['stats'][key] + float(change)
                # Extracted metadata may name the upgrade Weak while the
                # runtime serializes WeakPower. Keep both aliases in sync.
                alias = key[:-5] if key.endswith('power') else key + 'power'
                if alias in meta['stats']:
                    values[alias] = meta['stats'][alias] + float(change)
    observed = {k.lower(): v for k, v in (card.get('stats') or {}).items()
                if isinstance(v, (int, float))}
    values.update(observed)
    for key in tuple(values):
        if not key.endswith('power'):
            continue
        alias = key[:-5]
        if key in observed:
            values[alias] = observed[key]
        elif alias in observed:
            values[key] = observed[alias]
    return values


_DRAW = {'DEEP_STRETCH', 'ENCORE', 'KI_HOP', 'RUMMAGE', 'WAH_CHO_KI', 'WREST'}
_SADNESS = {'SORROWFUL_STRIKE': 2, 'JEST': 1, 'MOURNING_BLOW': 1,
            'RUMMAGE': 1, 'VITRIOL': 1, 'BRIGHTEN_UP': 2, 'SELF_DEPRECATE': 1, 'ENNUI': 1}
_DELAYED_ENERGY = {'KI_REST', 'KI_WHIP', 'WAH_KI'}
_IMPALED_ENERGY = {'BOW_SMACK', 'EPIC_RETRIEVAL'}
# Values for effects whose return cannot be read as damage/block/draw.
_ENGINE_BASE = {'ARCHERY': 7.0, 'HIDDEN_ROOTS': 7.4, 'MYTHIC_FORM': 8.3,
                'QUIVER': 6.0, 'MOTIF': 5.4, 'SONG_PATH': 5.6,
                'LULLABY': 4.4, 'VIOLENT_CHORD': 5.5, 'COVERING_FIRE': 1.0,
                'ALL_IS_WELL': 3.5, 'ECSTASY': 3.5, 'ENNUI': 7.8,
                'SERRATED_ARROWS': 6.0, 'EXPLOSIVE_ARROWS': 5.8,
                'HONING': 5.8, 'YAH': 5.6, 'SIMPLETON': 6.5,
                'LOST_LUTE': 7.3, 'TUNE': 5.5, 'COMPOST': 2.0,
                'NOCK': 5.3, 'BRACE': 5.8, 'SHOW_OFF': 6.3,
                'KI_VERSE': 4.0, 'QUIETUS': 4.0, 'BLOT_OUT_THE_SKY': 5.0,
                'LOCKED_IN': 4.5, 'RECALL': 3.8, 'UPROOT': 4.0,
                'FLORESCENCE': 3.2, 'BRIGHTEN_UP': 4.0, 'SELF_DEPRECATE': 1.0,
                'VITRIOL': 5.8,  # free two-turn Weak offsets the Sadness cost
                'ENSEMBLE': 1.0, 'ENRAPTURE': 1.0}


def effects(card, deck=None):
    """Per-play equivalent effects, before deck synergy (never mutates input)."""
    cid = card_id(card)
    meta = METADATA[cid]
    s = _stats(card)
    deck = deck or []
    melodies = set().union(*(melody_types(c) for c in deck)) if deck else set()
    melody_count = sum(bool(melody_types(c)) for c in deck)
    generators = sum(_SADNESS.get(card_id(c), 0) for c in deck)
    cost = card.get('cost', meta['cost'])
    if not isinstance(cost, (int, float)):
        cost = meta['cost']
    if card.get('upgraded') and 'cost' not in card and 'cost' in meta['upgrade']:
        cost = meta['upgrade']['cost']
    damage = s.get('damage', s.get('calculationbase', 0))
    hits = s.get('hitcount', 1)
    # Persistent Melodies ramp up during a fight. Use 75% of the eventual
    # supported count, rather than assuming all three exist on turn one.
    active = len(melodies) * .75
    if cid in {'MELODIC_SHOT', 'RHYTHM_STRIKE'}:
        hits += active
    if cid == 'WAH_BLAST' and melodies & {'CHO', 'KI'}:
        hits += .75
    if cid == 'WAH_SYMPHONY':
        damage += s.get('extradamage', 4) * melody_count
    if cid == 'ANGUISH':
        damage += s.get('extradamage', 6) * min(generators * .5, 3)
    if cid == 'CODA':
        cost = max(cost - 2 * active, 1)
    if cid == 'CHO_LIMBS' and melodies & {'WAH', 'KI'}:
        cost = 0

    if cid in {'REPEAT_SHOT'}:
        hits = 2  # X=2 proxy, not a free 0-energy shot.
        cost = 2
    block = s.get('block', 0)
    if cid == 'HARMONIC_SHIELD':
        block = s.get('calculationextra', 5) * active
    if cid == 'CHO_FIRM':
        block *= 1.65  # extra block only while an Arrow is available
    if cid == 'CHO_LIMBS':
        block *= 2
    draw = s.get('cards', 0) if cid in _DRAW else 0
    if cid == 'KI_CHANT':
        draw = 1  # looks at N; only ONE enters Hand
    if cid in {'KI_HOP', 'WREST'}:
        draw *= .65  # Impaled/Dislodge prerequisite
    if cid == 'STEADY_CADENCE':
        draw = active * s.get('calculationextra', 1)
    if cid == 'WAH_KI':
        draw = .6  # next turn
    energy = s.get('energy', 0)
    if cid in _DELAYED_ENERGY:
        energy *= .5
    elif cid in _IMPALED_ENERGY:
        energy *= .5
    elif cid not in {'ENCORE'}:
        energy = 0  # powers/conditional triggers scored as engines
    stretch = s.get('stretchpower', 0)
    if cid == 'GROWTH':
        stretch = s.get('calculationextra', 4) * min(generators * .5, 3)
    if cid == 'BLOSSOM_SHOT':
        stretch = damage
    # Stretch is consumed by a later card's Block; discount failure to convert.
    defense = block + .7 * stretch
    return {'attack': damage * hits, 'defense': defense, 'draw': draw,
            'energy': energy, 'aim': s.get('aimpower', 0),
            'cost': max(cost, 1), 'hits': hits, 'sadness': _SADNESS.get(cid, 0),
            'weak': s.get('weakpower', s.get('power', 0)) if meta['weak'] else 0,
            'vulnerable': s.get('vulnerablepower', s.get('power', 0)) if meta['vulnerable'] else 0}


def dimensions(card):
    e = effects(card)
    return {'attack': e['attack'] / e['cost'],
            'defense': e['defense'] / e['cost'],
            'draw': e['draw'], 'energy': e['energy']}


def base_score(card, deck=None):
    cid = card_id(card)
    e = effects(card, deck)
    m = METADATA[cid]
    if cid in _ENGINE_BASE:
        value = _ENGINE_BASE[cid]
        if card.get('upgraded'):
            value += .6
        if deck is not None:
            melodies = set().union(*(melody_types(c) for c in deck)) if deck else set()
            required = {'SONG_PATH': {'CHO'}, 'TUNE': {'KI'},
                        'VIOLENT_CHORD': {'WAH'}, 'LULLABY': {'WAH','CHO','KI'}}
            if cid in required and not required[cid] <= melodies:
                value = min(value, 2.0 + len(required[cid] & melodies) * .6)
            if cid in {'MOTIF', 'LOST_LUTE'} and not melodies:
                value = 2.5
    else:
        value = min(e['attack'] / e['cost'] / 6 * 5, 7)
        value += min(e['defense'] / e['cost'] / 5 * 4, 6)
        value += e['draw'] * 1.4 + e['energy'] * 1.7 + e['aim'] * .25
        value += .7 * m['weak'] + .8 * m['vulnerable'] + .7 * m['aoe']
        value -= e['sadness'] * .9
        if cid in {'ENCORE'}:
            value -= 2.5  # hard restriction: only one further card this turn
        if cid == 'KI_CHANT':
            value += 4.2  # selection + discard, not three cards drawn
        if cid == 'STRIKE_FOOL':
            value = min(value, 2.0)
        elif cid == 'DEFEND_FOOL':
            value = min(value, 2.5)
    return max(0., min(value, 10.))


def melody_types(card):
    cid = card_id(card)
    return {v for v in ('WAH', 'CHO', 'KI') if v in cid.split('_')}


def deck_bonus(card, deck):
    """Bounded support bonuses; no inherited Ironclad archetype penalties."""
    cid = card_id(card)
    counts = Counter(card_id(c) for c in deck)
    melodies = set().union(*(melody_types(c) for c in deck)) if deck else set()
    melody_count = sum(bool(melody_types(c)) for c in deck)
    generators = sum(_SADNESS.get(card_id(c), 0) for c in deck)
    payoff = sum(counts[c] for c in ('ALL_IS_WELL','ECSTASY','ANGUISH','GROWTH'))
    shots = sum(METADATA.get(card_id(c), {}).get('shot', False) for c in deck)
    aim = sum(counts[c] for c in ('ARCHERY','NOCK','BRACE','CHO_READY','SHOW_OFF'))
    bonus = 0.
    own = melody_types(card)
    if own:
        bonus += .45 * len(own - melodies) + min(.45, melody_count * .05)
    if cid in {'LULLABY','MOTIF','SONG_PATH','VIOLENT_CHORD','KI_VERSE','CODA',
               'HARMONIC_SHIELD','MELODIC_SHOT','WAH_SYMPHONY','CHO_CHORUS',
               'STEADY_CADENCE','RHYTHM_STRIKE'}:
        bonus += len(melodies) * .7 + min(melody_count * .12, 1.)
    if ((cid == 'SONG_PATH' and 'CHO' not in melodies)
            or (cid == 'VIOLENT_CHORD' and 'WAH' not in melodies)):
        bonus = 0.
    if cid == 'LULLABY' and len(melodies) < 3:
        bonus = 0.
    if cid in {'MOTIF', 'LOST_LUTE'} and not melodies:
        bonus = 0.
    if cid == 'WAH_CHO_KI':
        bonus += 1.0
    if cid in {'DEEP_STRETCH','HIDDEN_ROOTS','CHO_KI','BOUNCE','BLOSSOM_SHOT'}:
        bonus += .6 + min(counts['CHO_FIRM'] * .25, .5)
    if cid in {'ALL_IS_WELL','ECSTASY','ANGUISH','GROWTH','PUNCH_OF_JOY'}:
        bonus += min(generators * .5, 2.)
    if cid in _SADNESS:
        bonus += min(payoff * .6, 1.8)
    if METADATA[cid]['shot']:
        bonus += min(aim * .35 * min(effects(card)['hits'], 3), 1.8)
        bonus += min(counts['QUIVER'] * .7, 1.4)
        # An Arrow takes three further hits to Dislodge. Extra shots compete
        # for it; never treat every shot in a hand as immediately playable.
        bonus -= min(max(shots - 3 - counts['QUIVER'] * 2, 0) * .35, 1.4)
    if cid == 'QUIVER':
        bonus += min(shots * .45, 2.)
    if cid in {'ARCHERY','NOCK','BRACE','SHOW_OFF'}:
        bonus += min(shots * .4, 1.6)
    if counts[cid] and METADATA[cid]['type'] == 'Attack' and not own:
        bonus -= min(counts[cid] * .6, 1.8)
    if counts[cid] and METADATA[cid]['type'] == 'Power':
        bonus -= min(counts[cid] * 1.5, 3.)
    # A small dilution cost, rather than stacking three unrelated lean-deck
    # penalties. Core block/engine cards can still clear reward thresholds.
    bonus -= min(max(len(deck) - 25, 0) * .05, .75)
    return max(-3., min(bonus, 4.))


def score_in_deck(card, deck):
    return max(0., min(base_score(card, deck) + deck_bonus(card, deck), 10.))


def upgrade_value(card, deck):
    """Value the actual upgrade delta, including cost and card draw changes."""
    after = card.get('after_upgrade')
    if card.get('upgraded') or not isinstance(after, dict):
        return None
    upgraded = {**card, **after, 'upgraded': True,
                'stats': {**(card.get('stats') or {}), **(after.get('stats') or {})}}
    def metrics(value):
        if is_fool(value):
            return effects(value, deck)
        stats = value.get('stats') or {}
        return {'attack': stats.get('damage', 0) * stats.get('hitcount', 1),
                'defense': stats.get('block', 0), 'draw': stats.get('cards', stats.get('draw', 0)),
                'aim': 0, 'energy': stats.get('energy', 0)}
    before = metrics(card)
    after_effects = metrics(upgraded)
    saving = max(0, card.get('cost', METADATA.get(card_id(card), {}).get('cost', 1)) -
                 upgraded.get('cost', METADATA.get(card_id(card), {}).get('cost', 1)))
    value = saving * 3.5
    value += (after_effects['attack'] - before['attack']) * .3
    value += (after_effects['defense'] - before['defense']) * .4
    value += (after_effects['draw'] - before['draw']) * 2
    value += (after_effects['aim'] - before['aim']) * .4
    value += (after_effects['energy'] - before['energy']) * 1.5
    value += (after_effects.get('weak', 0) - before.get('weak', 0)) * 1.2
    value += (after_effects.get('vulnerable', 0) - before.get('vulnerable', 0)) * 1.2
    if is_fool(card) and METADATA[card_id(card)]['type'] == 'Power':
        recurring = {'HIDDEN_ROOTS': ('hiddenrootspower', .4),
                     'ARCHERY': ('aimpower', .4), 'SONG_PATH': ('songpathpower', .4),
                     'VIOLENT_CHORD': ('violentchordpower', .3)}
        if card_id(card) in recurring:
            key, weight = recurring[card_id(card)]
            value += max(0, _stats(upgraded).get(key, 0) - _stats(card).get(key, 0)) * 3 * weight
        value += max(0, score_in_deck(upgraded, deck) - score_in_deck(card, deck)) * 2
    return value


def best_upgrade(cards, deck, trace=None):
    values = [(i, upgrade_value(c, deck)) for i, c in enumerate(cards)]
    values = [(i, value) for i, value in values if value is not None]
    if trace is not None:
        for i, value in values:
            trace.option(i, kind='card', score=value, components=[
                ('card.base', value, 'Fool upgrade delta'),
            ])
    return max(values, key=lambda pair: pair[1])[0] if values else None
