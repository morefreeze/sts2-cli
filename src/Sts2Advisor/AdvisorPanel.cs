// src/Sts2Advisor/AdvisorPanel.cs
using Godot;
using MegaCrit.Sts2.Core.Localization.Fonts;
using MegaCrit.Sts2.Core.Nodes;
using Sts2Advisor.Core;

namespace Sts2Advisor;

/// <summary>Bottom-right overlay. Stock Godot nodes only -- no C# node subclasses, so no Godot
/// source generators or script registration. Ignores the mouse: it never blocks a click.</summary>
internal sealed class AdvisorPanel
{
    private readonly CanvasLayer _layer;
    private readonly PanelContainer _panel;
    private readonly RichTextLabel _text;
    private string? _fontLanguage = "";
    private string? _shownText;

    private AdvisorPanel(CanvasLayer layer, PanelContainer panel, RichTextLabel text)
    {
        _layer = layer;
        _panel = panel;
        _text = text;
    }

    public bool Alive => GodotObject.IsInstanceValid(_layer) && GodotObject.IsInstanceValid(_text);

    /// <summary>Null until the game's root node exists.</summary>
    public static AdvisorPanel? TryCreate()
    {
        var host = NGame.Instance;
        if (host == null || !GodotObject.IsInstanceValid(host))
            return null;
        var layer = new CanvasLayer { Name = "Sts2AdvisorOverlay", Layer = 110 };
        var panel = new PanelContainer { Name = "Panel", MouseFilter = Control.MouseFilterEnum.Ignore };
        var box = new StyleBoxFlat { BgColor = new Color(0.06f, 0.06f, 0.08f, 0.82f) };
        box.SetCornerRadiusAll(8);
        box.SetContentMarginAll(12);
        panel.AddThemeStyleboxOverride("panel", box);
        var text = new RichTextLabel
        {
            Name = "Text",
            BbcodeEnabled = true,
            FitContent = true,
            ScrollActive = false,
            MouseFilter = Control.MouseFilterEnum.Ignore,
            AutowrapMode = TextServer.AutowrapMode.WordSmart,
            CustomMinimumSize = new Vector2(380, 0),
        };
        text.AddThemeFontSizeOverride("normal_font_size", 15);
        text.AddThemeFontSizeOverride("bold_font_size", 15);
        panel.AddChild(text);
        layer.AddChild(panel);
        panel.SetAnchorsAndOffsetsPreset(Control.LayoutPreset.BottomRight, Control.LayoutPresetMode.Minsize, 16);
        panel.GrowHorizontal = Control.GrowDirection.Begin;
        panel.GrowVertical = Control.GrowDirection.Begin;
        panel.Visible = false;
        host.CallDeferred(Node.MethodName.AddChild, layer);
        host.TreeExiting += SidecarProcess.Stop;
        return new AdvisorPanel(layer, panel, text);
    }

    public void Show(IReadOnlyList<PanelLine> lines, string? language)
    {
        if (!Alive) return;
        if (language != _fontLanguage)
        {
            _fontLanguage = language;
            // The game's CJK substitute font; a no-op for languages that need none.
            _text.ApplyLocaleFontSubstitution(FontType.Regular, "normal_font");
            _text.ApplyLocaleFontSubstitution(FontType.Bold, "bold_font");
            _shownText = null;
        }
        // Status lines are re-shown on every 0.3 s poll; only touch the label when the text changed.
        string bbcode = string.Join("\n", lines.Select(Bbcode));
        if (bbcode != _shownText)
        {
            _text.Text = bbcode;
            _shownText = bbcode;
        }
        _panel.Visible = true;
    }

    public void Hide()
    {
        if (Alive) _panel.Visible = false;
    }

    private static string Bbcode(PanelLine line)
    {
        string t = line.Text.Replace("[", "[lb]");
        return line.Style switch
        {
            LineStyle.Title => $"[color=#a8a8b3][font_size=13]{t}[/font_size][/color]",
            LineStyle.Pick => $"[b][color=#7fc8ff]{t}[/color][/b]",
            LineStyle.Fired => $"[color=#ffffff]{t}[/color]",
            LineStyle.Normal => $"[color=#d9d9e0]{t}[/color]",
            LineStyle.Muted => $"[color=#8a8a96]{t}[/color]",
            LineStyle.Warning => $"[color=#ffc66b]{t}[/color]",
            _ => t,
        };
    }
}
