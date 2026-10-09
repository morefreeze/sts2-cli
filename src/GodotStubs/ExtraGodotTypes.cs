namespace Godot;

// Missing Godot node types
public partial class Line2D : Node2D
{
    public Vector2[] Points { get; set; } = Array.Empty<Vector2>();
    public float Width { get; set; } = 1f;
    public Color DefaultColor { get; set; } = Color.White;
    public void AddPoint(Vector2 position, int? atPosition = null) { }
    public void ClearPoints() { }
}

public partial class CpuParticles2D : Node2D
{
    public int Amount { get; set; }
    public bool Emitting { get; set; }
    public float Lifetime { get; set; }
    public bool OneShot { get; set; }
    public double SpeedScale { get; set; }
    public void Restart() { }
}

public partial class Marker2D : Node2D { }
public partial class PathFollow2D : Node2D
{
    public float Progress { get; set; }
    public float ProgressRatio { get; set; }
}
public partial class Path2D : Node2D { }

public partial class BackBufferCopy : Node2D { }
public partial class CanvasGroup : Node2D { }
public partial class CanvasItemMaterial : Material { }

public partial class NinePatchRect : Control
{
    public Texture2D? Texture { get; set; }
}

public partial class AspectRatioContainer : Container { }
public partial class VFlowContainer : FlowContainer { }

public partial class WorldEnvironment : Node { }
public partial class FastNoiseLite : Noise { }

public partial class Font : Resource
{
    public float GetStringSize(string text, int alignment = 0, float width = -1, int fontSize = 16) => text.Length * fontSize * 0.6f;
}

public partial class TextParagraph : GodotObject
{
    public void Clear() { }
    public void AddString(string text, Font font, int fontSize) { }
    public Vector2 GetSize() => Vector2.Zero;
    public float GetWidth() => 0;
}

public partial class StyleBoxEmpty : StyleBox { }

public partial class GradientTexture2D : Texture2D { }
public partial class Gradient : Resource { }

public partial class ParticleProcessMaterial : Material
{
    public Vector3 EmissionBoxExtents { get; set; }
}

public partial class RenderingServer
{
    public enum ViewportMsaa : long { Disabled = 0, Msaa2X = 1, Msaa4X = 2, Msaa8X = 3 }
    public static void GlobalShaderParameterSet(StringName name, Variant value) { }
}

// Input types
public enum Key : long { None = 0, A = 65, B = 66, C = 67, D = 68, E = 69, F = 70, G = 71, H = 72, I = 73, J = 74, K = 75, L = 76, M = 77, N = 78, O = 79, P = 80, Q = 81, R = 82, S = 83, T = 84, U = 85, V = 86, W = 87, X = 88, Y = 89, Z = 90, Escape = 4194305, Enter = 4194309, Tab = 4194306, Space = 32, Left = 4194319, Right = 4194321, Up = 4194320, Down = 4194322, F1 = 4194332, F2 = 4194333, F3 = 4194334, F4 = 4194335, F5 = 4194336, F6 = 4194337, F7 = 4194338, F8 = 4194339, F9 = 4194340, F10 = 4194341, F11 = 4194342, F12 = 4194343 }
public enum MouseButton : long { None = 0, Left = 1, Right = 2, Middle = 3, WheelUp = 4, WheelDown = 5 }
public partial class InputEventJoypadMotion : InputEvent
{
    public JoyAxis Axis { get; set; }
    public float AxisValue { get; set; }
}
public partial class InputEventAction : InputEvent
{
    public StringName Action { get; set; } = "";
}

// Error enum
public enum Error : long
{
    Ok = 0,
    Failed = 1,
    Unavailable = 2,
    Unconfigured = 3,
    Unauthorized = 4,
    ParameterRangeError = 5,
    OutOfMemory = 6,
    FileNotFound = 7,
    FileBadDrive = 8,
    FileBadPath = 9,
    FileNoPermission = 10,
    FileAlreadyInUse = 11,
    FileCantOpen = 12,
    FileCantWrite = 13,
    FileCantRead = 14,
    FileUnrecognized = 15,
    FileCorrupt = 16,
    FileMissingDependencies = 17,
    FileEof = 18,
    CantOpen = 19,
    CantCreate = 20,
    QueryFailed = 21,
    AlreadyInUse = 22,
    Locked = 23,
    Timeout = 24,
    CantConnect = 25,
    CantResolve = 26,
    ConnectionError = 27,
    CantAcquireResource = 28,
    CantFork = 29,
    InvalidData = 30,
    InvalidParameter = 31,
    AlreadyExists = 32,
    DoesNotExist = 33,
    DatabaseCantRead = 34,
    DatabaseCantWrite = 35,
    CompilationFailed = 36,
    MethodNotFound = 37,
    LinkFailed = 38,
    ScriptFailed = 39,
    CyclicLink = 40,
    InvalidDeclaration = 41,
    DuplicateSymbol = 42,
    ParseError = 43,
    Busy = 44,
    Skip = 45,
    Help = 46,
    Bug = 47
}

// Tool attribute
[AttributeUsage(AttributeTargets.Class)]
public partial class ToolAttribute : Attribute { }

// ExportToolButton attribute
[AttributeUsage(AttributeTargets.Property | AttributeTargets.Field)]
public partial class ExportToolButtonAttribute : Attribute
{
    public ExportToolButtonAttribute(string text, string icon = "") { }
}

// AssemblyHasScripts attribute
[AttributeUsage(AttributeTargets.Assembly)]
public partial class AssemblyHasScriptsAttribute : Attribute
{
    public AssemblyHasScriptsAttribute() { }
    public AssemblyHasScriptsAttribute(string[] scripts) { }
}

// Signal class (not attribute) - note: this conflicts with Signal attribute,
// but decompiled code uses both patterns
// public class Signal { public Signal(GodotObject owner, StringName name) { } }

// Colors - static color constants
public static partial class Colors
{
    public static Color White { get; } = Color.White;
    public static Color Black { get; } = Color.Black;
    public static Color Red { get; } = new(1, 0, 0);
    public static Color Green { get; } = new(0, 1, 0);
    public static Color Blue { get; } = new(0, 0, 1);
    public static Color Yellow { get; } = new(1, 1, 0);
    public static Color Transparent { get; } = Color.Transparent;
    public static Color Orange { get; } = new(1, 0.65f, 0);
    public static Color Purple { get; } = new(0.63f, 0.13f, 0.94f);
    public static Color Cyan { get; } = new(0, 1, 1);
    public static Color Magenta { get; } = new(1, 0, 1);
    public static Color Gray { get; } = new(0.5f, 0.5f, 0.5f);
    public static Color DarkGray { get; } = new(0.25f, 0.25f, 0.25f);
    public static Color LightGray { get; } = new(0.75f, 0.75f, 0.75f);
}

// Range control
public partial class Range : Control
{
    public new partial class SignalName : Control.SignalName
    {
        public static readonly StringName ValueChanged = "ValueChanged";
    }
    public double Value { get; set; }
    public double MinValue { get; set; }
    public double MaxValue { get; set; } = 100;
    public double Step { get; set; } = 1;
    public double Page { get; set; }
    public float Ratio { get; set; }
}

// _ProcessCustomFX for RichTextEffect needs this signature
// Already defined in UI.cs - just ensure it's virtual
