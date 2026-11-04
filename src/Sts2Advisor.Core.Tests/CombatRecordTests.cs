using System.Text.Json;
using Sts2Advisor.Core;

public class CombatRecordTests
{
    private static CombatRecord Full() => new()
    {
        Encounter = "ENCOUNTER.AEONGLASS_BOSS", RoomType = "Boss", Act = 3, Floor = 16, Won = true, Turns = 7,
        HpStart = 70, MaxHpStart = 80, BlockStart = 0, HpEnd = 52, MaxHpEnd = 80, BlockEnd = 4,
        GoldStart = 100, GoldEnd = 100,
        PotionsStart = new[] { "POTION.FIRE", "POTION.BLOCK" }, PotionsEnd = new[] { "POTION.BLOCK" },
    };

    [Fact]
    public void ToJsonRoundTripsEveryField()
    {
        using var doc = JsonDocument.Parse(Full().ToJson());
        var r = doc.RootElement;
        Assert.Equal("ENCOUNTER.AEONGLASS_BOSS", r.GetProperty("encounter").GetString());
        Assert.Equal("Boss", r.GetProperty("room_type").GetString());
        Assert.Equal(3, r.GetProperty("act").GetInt32());
        Assert.Equal(16, r.GetProperty("floor").GetInt32());
        Assert.True(r.GetProperty("won").GetBoolean());
        Assert.Equal(7, r.GetProperty("turns").GetInt32());
        Assert.Equal(70, r.GetProperty("hp_start").GetInt32());
        Assert.Equal(80, r.GetProperty("max_hp_start").GetInt32());
        Assert.Equal(0, r.GetProperty("block_start").GetInt32());
        Assert.Equal(52, r.GetProperty("hp_end").GetInt32());
        Assert.Equal(80, r.GetProperty("max_hp_end").GetInt32());
        Assert.Equal(4, r.GetProperty("block_end").GetInt32());
        Assert.Equal(18, r.GetProperty("damage_taken").GetInt32());
        Assert.Equal(100, r.GetProperty("gold_start").GetInt32());
        Assert.Equal(100, r.GetProperty("gold_end").GetInt32());
        Assert.Equal(new[] { "POTION.FIRE", "POTION.BLOCK" }, r.GetProperty("potions_start").EnumerateArray().Select(e => e.GetString()));
        Assert.Equal(new[] { "POTION.BLOCK" }, r.GetProperty("potions_end").EnumerateArray().Select(e => e.GetString()));
        Assert.Equal(new[] { "POTION.FIRE" }, r.GetProperty("potions_used").EnumerateArray().Select(e => e.GetString()));
    }

    [Fact]
    public void UnknownFieldsAreNullAndKeysAlwaysPresent()
    {
        var json = new CombatRecord { Won = false }.ToJson();
        Assert.DoesNotContain("\n", json);
        using var doc = JsonDocument.Parse(json);
        var r = doc.RootElement;
        Assert.False(r.GetProperty("won").GetBoolean());
        foreach (var key in new[] { "encounter", "room_type", "act", "floor", "turns", "hp_start", "max_hp_start", "block_start",
                                    "hp_end", "max_hp_end", "block_end", "damage_taken", "gold_start", "gold_end",
                                    "potions_start", "potions_end", "potions_used" })
            Assert.Equal(JsonValueKind.Null, r.GetProperty(key).ValueKind);
    }

    [Fact]
    public void DamageTakenNeedsBothHpValues()
    {
        Assert.Null(new CombatRecord { HpStart = 30 }.DamageTaken);
        Assert.Null(new CombatRecord { HpEnd = 30 }.DamageTaken);
        Assert.Equal(-5, new CombatRecord { HpStart = 30, HpEnd = 35 }.DamageTaken);  // healed by an end-of-combat relic
    }

    [Fact]
    public void PotionsUsedIsAMultisetDifference()
    {
        var r = new CombatRecord
        {
            PotionsStart = new[] { "A", "A", "B" },
            PotionsEnd = new[] { "A", "C" },  // C was gained mid-combat and is ignored
        };
        Assert.Equal(new[] { "A", "B" }, r.PotionsUsed);
        Assert.Empty(new CombatRecord { PotionsStart = new[] { "A" }, PotionsEnd = new[] { "A", "A" } }.PotionsUsed!);
    }

    [Fact]
    public void PotionsUsedIsNullWhenEitherSideIsUnknown()
    {
        Assert.Null(new CombatRecord { PotionsStart = new[] { "A" } }.PotionsUsed);
        Assert.Null(new CombatRecord { PotionsEnd = new[] { "A" } }.PotionsUsed);
    }
}
