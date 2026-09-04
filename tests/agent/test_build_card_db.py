"""The generator must handle the localization template grammar.

`localization_eng/cards.json` uses nested, piped templates:
    {Var:diff()}                     plain value
    {Var:energyIcons()}              value rendered as energy icons
    {Var:plural:one|{Var:diff()} many}   singular/plural, NESTED braces
    {IfUpgraded:show:A|B}            upgrade-conditional
    {InCombat:A|B}                   in-combat-conditional
A regex using [^}]* stops at the first closing brace and shreds these, which
produced text like "Prevent the next 1 times}" and "Draw 2 2" — malformed
strings that then parse to zero effects.
"""
from agent.sim.build_card_db import render


def test_renders_plain_value():
    text, unresolved = render("Deal {Damage:diff()} damage.", {"Damage": 8})
    assert text == "Deal 8 damage."
    assert unresolved == []


def test_renders_plural_with_nested_braces():
    out, _ = render(
        "Prevent the next {BufferPower:plural:time|{BufferPower:diff()} times}"
        " you would lose HP.",
        {"BufferPower": 1},
    )
    assert out == "Prevent the next time you would lose HP."


def test_plural_picks_the_many_branch_above_one():
    out, _ = render("Draw {Cards:diff()} {Cards:plural:card|cards}.", {"Cards": 2})
    assert out == "Draw 2 cards."


def test_energy_icons_render_the_noun_from_the_variable_name():
    """"Gain 2." loses what the 2 is OF, and the effect parser cannot recover it.

    The noun for the icon functions lives in the variable name, not the prose,
    unlike diff() where the prose carries it ("Gain {Block:diff()} Block").
    """
    out, _ = render("Gain {Energy:energyIcons()}.", {"Energy": 2})
    assert out == "Gain 2 Energy."


def test_star_icons_render_the_noun_and_respect_singular():
    assert render("Gain {Stars:starIcons()}.", {"Stars": 3})[0] == "Gain 3 Stars."
    assert render("Gain {Stars:starIcons()}.", {"Stars": 1})[0] == "Gain 1 Star."


def test_diff_stays_a_bare_number_because_the_prose_has_the_noun():
    out, _ = render("Gain {Block:diff()} Block.", {"Block": 8})
    assert out == "Gain 8 Block."


def test_if_upgraded_takes_the_base_branch():
    out, _ = render("Add {IfUpgraded:show:{Cards} copies|a copy} of that card.",
                    {"Cards": 2})
    assert out == "Add a copy of that card."


def test_in_combat_takes_the_base_branch():
    out, _ = render("Deal damage.{InCombat:(Deals {X:diff()} damage)|}", {"X": 5})
    assert out == "Deal damage."


def test_markup_is_stripped_but_content_kept():
    out, _ = render("[gold]Channel[/gold] 1 [gold]Frost[/gold].", {})
    assert out == "Channel 1 Frost."
