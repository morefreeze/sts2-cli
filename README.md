# sts2-cli

Headless Slay the Spire 2 CLI. Play the full game from a terminal — no GPU, no UI, no Godot runtime needed.

**Use cases:**
- Play interactively in your terminal
- Build an LLM agent that plays the game
- RL training environment
- Automated testing / game balance analysis

**Performance:** 100/100 games complete, ~1s/game, 0 crashes.

## Quick Start

```bash
# 1. Setup: copy game DLLs + apply IL patches + build
./setup.sh

# 2. Play interactively (Chinese by default)
python3 python/play.py

# Play in English
python3 python/play.py --lang en

# Auto-play (simple AI)
python3 python/play.py --auto --seed test123

# Run 100 games with smart agent
python3 python/smart_agent.py 100
```

## Interactive Mode

```
$ python3 python/play.py --character Ironclad

Slay the Spire 2 — Headless CLI
Character: Ironclad  Seed: random

═══════════════════════════════════════════════════
  Overgrowth(密林) Floor 0
  The Ironclad(铁甲战士)  HP ████████████████████ 80/80  Gold 99  Deck 10
  Relics: Burning Blood(燃烧之血)

     0   1   2   3   4   5   6
  ──────────────────────────────
  15|  R           R           R
  14|  ?       E   M   E
   ...
   1|      M       M           M
  ──────────────────────────────
  M=怪 E=英 R=休 $=店 T=宝 ?=事 [x]=你 x=可选

  可选路径:
    [0] ⚔ 怪物
    [1] ⚔ 怪物
    [2] ⚔ 怪物

> 选择路径 [编号]: 0

──────────────────────────────────────────────────
  回合 1  能量3/3  抽牌5  弃牌0
  铁甲战士  HP ████████████████████ 80/80  金99  牌组10

  [0] 小啃兽  ████████████████████ 44/44  ⚔12

  ● [0] 防御 (1) 5挡
  ● [1] 打击 (1) 6伤  →
  ● [2] 打击 (1) 6伤  →
  ● [3] 防御 (1) 5挡
  ● [4] 痛击 (2) 8伤  →

> 出牌 [编号], (e)结束回合, (p0)药水: 4
```

## JSON Protocol

For programmatic use, communicate via JSON over stdin/stdout:

### Commands (stdin)

```jsonc
// Start a new run
{"cmd": "start_run", "character": "Ironclad", "seed": "my_seed", "ascension": 0}

// Perform an action (response is always the next decision point)
{"cmd": "action", "action": "<action_name>", "args": {...}}

// Quit
{"cmd": "quit"}
```

### Decision Points (stdout)

The CLI drives the game forward and pauses at every **decision point** — a moment where the player must choose. Each response is a JSON object with `"decision"` field:

| Decision | When | Available Actions |
|---|---|---|
| `map_select` | At the map, choose next room | `select_map_node` |
| `combat_play` | Your turn in combat | `play_card`, `end_turn` |
| `card_reward` | After combat, pick a card | `select_card_reward`, `skip_card_reward` |
| `rest_site` | At a campfire | `choose_option` |
| `event_choice` | Random event | `choose_option`, `leave_room` |
| `shop` | At the merchant | `buy_card`, `buy_relic`, `buy_potion`, `remove_card`, `leave_room` |
| `game_over` | Run ended | *(terminal state)* |

### Actions Reference

```jsonc
// Map: select a node from the choices list
{"cmd": "action", "action": "select_map_node", "args": {"col": 3, "row": 1}}

// Combat: play a card (target_index required for AnyEnemy cards)
{"cmd": "action", "action": "play_card", "args": {"card_index": 0, "target_index": 0}}

// Combat: end your turn
{"cmd": "action", "action": "end_turn"}

// Card Reward: pick a card by index
{"cmd": "action", "action": "select_card_reward", "args": {"card_index": 1}}

// Card Reward: skip (take no card)
{"cmd": "action", "action": "skip_card_reward"}

// Rest Site / Event: choose an option by index
{"cmd": "action", "action": "choose_option", "args": {"option_index": 0}}

// Shop: buy items
{"cmd": "action", "action": "buy_card", "args": {"card_index": 2}}
{"cmd": "action", "action": "buy_relic", "args": {"relic_index": 0}}
{"cmd": "action", "action": "buy_potion", "args": {"potion_index": 1}}
{"cmd": "action", "action": "remove_card"}

// Leave current room (shop, event)
{"cmd": "action", "action": "leave_room"}
```

### Example: combat_play Response

All names are bilingual `{"en": "...", "zh": "..."}`:

```json
{
  "type": "decision",
  "decision": "combat_play",
  "round": 1,
  "energy": 3,
  "max_energy": 3,
  "hand": [
    {
      "index": 0,
      "id": "CARD.STRIKE_IRONCLAD",
      "name": {"en": "Strike", "zh": "打击"},
      "cost": 1,
      "type": "Attack",
      "can_play": true,
      "target_type": "AnyEnemy",
      "description": {"en": "Deal {Damage:diff()} damage.", "zh": "造成{Damage:diff()}点伤害。"}
    }
  ],
  "enemies": [
    {
      "index": 0,
      "name": {"en": "Nibbit", "zh": "小啃兽"},
      "hp": 44,
      "max_hp": 44,
      "block": 0,
      "intends_attack": true
    }
  ],
  "player": {
    "name": {"en": "The Ironclad", "zh": "铁甲战士"},
    "hp": 80,
    "max_hp": 80,
    "block": 0,
    "gold": 99,
    "relics": [{"en": "Burning Blood", "zh": "燃烧之血"}],
    "potions": [],
    "deck_size": 10
  },
  "draw_pile_count": 5,
  "discard_pile_count": 0
}
```

## Architecture

```
Your Code (Python/JS/LLM/anything)
    │  JSON stdin/stdout
    ▼
Sts2Headless (C# .NET)
    │  Uses RunSimulator.cs for game lifecycle
    ▼
sts2.dll (game engine, IL-patched for headless)
    +  GodotStubs (GodotSharp.dll replacement)
    +  Harmony patches (localization fallbacks)
```

The game engine runs real STS2 game logic — all damage calculations, card effects, enemy AI, relic triggers, and RNG are identical to the actual game. The only differences:

- No rendering/audio (GodotStubs provides no-op implementations)
- `Task.Yield()` patched for synchronous execution
- Localization uses fallback keys (no PCK decryption at runtime)

## Game Data

Bilingual localization data extracted from the game:

- `localization_eng/` — 45 tables, English
- `localization_zhs/` — 45 tables, Simplified Chinese

## Prerequisites

- **Slay the Spire 2** installed via Steam
- **.NET 9+ SDK** ([download](https://dotnet.microsoft.com/download))
- **Python 3.9+** (for play.py)

Run `./setup.sh` to automatically copy game DLLs, apply IL patches, and build.

## Characters

| Character | EN | ZH | Starting HP | Starting Relic |
|---|---|---|---|---|
| Ironclad | The Ironclad | 铁甲战士 | 80 | Burning Blood (燃烧之血) |
| Silent | The Silent | 沉默猎手 | 70 | Ring of the Snake (蛇戒) |
| Defect | The Defect | 故障机器人 | 75 | Cracked Core (破碎核心) |
| Regent | The Regent | 摄政王 | 75 | Royal Decree (皇家法令) |

## Map Structure

The game has 4 Acts, each with 13-15 floors + boss:

| Act | EN | ZH | Floors |
|---|---|---|---|
| 1 | Overgrowth | 密林 | 15 + Boss |
| 2 | Hive | 巢穴 | 14 + Boss |
| 3 | Underdocks | 暗港 | 15 + Boss |
| 4 | Glory | 荣耀 | 13 + Boss |
