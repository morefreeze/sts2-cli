# STS2 CLI Learning Notes

## Known Issues
1. **Neow event skipped** — `StartedWithNeow=false`，缺少 Neow 的3个祝福选择。需要重新启用。
2. **Gold changes at rest site** — 不是 bug，是游戏 hook 机制（modifier/relic 可能在休息站加减 gold）
3. **1.5% stuck rate** — 极端边缘情况的异步问题，可接受
4. **Card descriptions show template vars** — `{Damage:diff()}` 未解析，但 stats 字段有真实数值

## Game Mechanics Learned
- Strike = 6 damage, cost 1
- Defend = 5 block, cost 1
- Bash = 8 damage + 2 Vulnerable, cost 2
- Burning Blood = heal 6 HP after combat
- Rest site heal = 30% max HP (capped at max, hooks may modify)
- Starting deck: 5 Strike + 4 Defend + 1 Bash = 10 cards
- Starting HP: 80, Energy: 3, Gold: 99

## Strategy Notes
- Random agent dies at floor 4-9 average
- Smart agent (play attacks > defends, bash first for vulnerable) dies at floor 7-12
- Need Neow blessings to have a better start
- Card rewards: Powers are high value (persistent effects)
- Rest when HP < 65%, otherwise Smith to upgrade

## TODO
- [ ] Re-enable Neow event with localization
- [ ] Track powers/buffs/debuffs in combat state
- [ ] Implement potion usage in combat
- [ ] Try to beat Act 1 Boss
