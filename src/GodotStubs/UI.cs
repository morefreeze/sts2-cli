namespace Godot;

// CanvasItem
public partial class CanvasItem : Node
{
    public Color Modulate { get; set; } = Color.White;
    public Color SelfModulate { get; set; } = Color.White;
    public bool Visible { get; set; } = true;

    // Method forms of the modulate properties. The engine calls these directly
    // (TestSubject.SetColor does), and without them the whole calling method fails
    // to resolve with MissingMethodException — which, from inside the combat turn
    // loop, kills the loop and strands the combat.
    public void SetSelfModulate(Color color) => SelfModulate = color;
    public Color GetSelfModulate() => SelfModulate;
    public void SetModulate(Color color) => Modulate = color;
    public Color GetModulate() => Modulate;
    public virtual void Show() => Visible = true;
    public virtual void Hide() => Visible = false;
    public bool IsVisibleInTree() => Visible;
    public Tween CreateTween() => new Tween();
    public Rect2 GetViewportRect() => new Rect2(Vector2.Zero, new Vector2(1920, 1080));
}

// Control
public partial class Control : CanvasItem
{
    public enum FocusModeEnum : long { None = 0, Click = 1, All = 2 }
    public enum MouseFilterEnum : long { Stop = 0, Pass = 1, Ignore = 2 }
    public enum LayoutPreset : long { TopLeft = 0, TopRight = 1, BottomLeft = 2, BottomRight = 3, FullRect = 15 }

    public new partial class MethodName : Node.MethodName { }
    public new partial class PropertyName : Node.PropertyName { }
    public new partial class SignalName : Node.SignalName
    {
        public static readonly StringName FocusEntered = "FocusEntered";
        public static readonly StringName FocusExited = "FocusExited";
        public static readonly StringName MouseEntered = "MouseEntered";
        public static readonly StringName MouseExited = "MouseExited";
        public static readonly StringName Resized = "Resized";
    }

    public Vector2 Position { get; set; }
    public Vector2 GlobalPosition { get; set; }
    public Vector2 Size { get; set; }
    public Vector2 CustomMinimumSize { get; set; }
    public float Rotation { get; set; }
    public Vector2 Scale { get; set; } = Vector2.One;
    public Vector2 PivotOffset { get; set; }
    public FocusModeEnum FocusMode { get; set; }
    public MouseFilterEnum MouseFilter { get; set; }
    public string TooltipText { get; set; } = "";

    public Rect2 GetViewportRect() => new Rect2(0, 0, 1920, 1080);
    public void GrabFocus() { }
    public void ReleaseFocus() { }
    public bool HasFocus() => false;
    public Viewport? GetViewport() => null;

    public virtual void _GuiInput(InputEvent @event) { }
}

// Node2D
public partial class Node2D : CanvasItem
{
    public new partial class MethodName : Node.MethodName { }
    public new partial class PropertyName : Node.PropertyName { }
    public new partial class SignalName : Node.SignalName { }

    public Vector2 Position { get; set; }
    public Vector2 GlobalPosition { get; set; }
    public float Rotation { get; set; }
    public float RotationDegrees { get; set; }
    public Vector2 Scale { get; set; } = Vector2.One;
    public Transform2D GlobalTransform { get; set; }
    public Transform2D Transform { get; set; }
}

// Resource
public partial class Resource : GodotObject
{
    public string ResourcePath { get; set; } = "";
    public partial class MethodName { }
    public partial class PropertyName { }
    public partial class SignalName { }
}

// PackedScene
public partial class PackedScene : Resource
{
    public enum GenEditState : long { Disabled = 0, Instance = 1, Main = 2 }
    public T Instantiate<T>(GenEditState editState = GenEditState.Disabled) where T : Node, new() => new T();
    public Node Instantiate(GenEditState editState = GenEditState.Disabled) => new Node();
}

// Texture types
public partial class Texture2D : Resource { }
public partial class CompressedTexture2D : Texture2D { }
public partial class AtlasTexture : Texture2D
{
    public Rect2 Region { get; set; }
    public Texture2D? Atlas { get; set; }
}
public partial class ImageTexture : Texture2D { }

// Material types
public partial class Material : Resource { }
public partial class ShaderMaterial : Material
{
    public void SetShaderParameter(StringName param, Variant value) { }
    public Variant GetShaderParameter(StringName param) => default;
}
public partial class Shader : Resource { }

// Curve
public partial class Curve : Resource
{
    public float Sample(float offset) => 0f;
}

// Tween
public partial class Tween : GodotObject
{
    public enum EaseType : long { In = 0, Out = 1, InOut = 2, OutIn = 3 }
    public enum TransitionType : long { Linear = 0, Sine = 1, Quint = 2, Quart = 3, Quad = 4, Expo = 5, Elastic = 6, Cubic = 7, Circ = 8, Bounce = 9, Back = 10, Spring = 11 }

    public new partial class SignalName : GodotObject.SignalName
    {
        public static readonly StringName Finished = "finished";
    }

    public event Action? Finished;

    public PropertyTweener TweenProperty(GodotObject obj, NodePath property, Variant finalVal, double duration) => new();
    public CallbackTweener TweenCallback(Callable callback) => new();
    public MethodTweener TweenMethod(Callable method, Variant from, Variant to, double duration) => new();
    public IntervalTweener TweenInterval(double time) => new();
    public Tween Parallel() => this;
    public Tween SetParallel(bool parallel = true) => this;
    public Tween SetLoops(int loops = 0) => this;
    public Tween SetEase(EaseType ease) => this;
    public Tween SetTrans(TransitionType trans) => this;
    public Tween Chain() => this;
    public bool CustomStep(double delta) { Finished?.Invoke(); return true; }
    public void Play() { Finished?.Invoke(); }
    public void Stop() { }
    public void Pause() { }
    public void Kill() { }
    public bool IsRunning() => false;
    public bool IsValid() => true;
    public Tween BindNode(Node node) => this;
    public Tween SetSpeedScale(float scale) => this;
    public Tween SetProcessMode(ProcessModeEnum mode) => this;
    public enum ProcessModeEnum { Physics, Idle, Always }
}

public partial class PropertyTweener : GodotObject
{
    public PropertyTweener From(Variant value) => this;
    public PropertyTweener SetEase(Tween.EaseType ease) => this;
    public PropertyTweener SetTrans(Tween.TransitionType trans) => this;
    public PropertyTweener SetDelay(double delay) => this;
    public PropertyTweener AsRelative() => this;
}

public partial class CallbackTweener : GodotObject
{
    public CallbackTweener SetDelay(double delay) => this;
}

public partial class MethodTweener : GodotObject
{
    public MethodTweener SetEase(Tween.EaseType ease) => this;
    public MethodTweener SetTrans(Tween.TransitionType trans) => this;
    public MethodTweener SetDelay(double delay) => this;
}

public partial class IntervalTweener : GodotObject { }

// UI Controls
public partial class TextureRect : Control
{
    public new partial class MethodName : Control.MethodName { }
    public new partial class PropertyName : Control.PropertyName { }
    public new partial class SignalName : Control.SignalName { }
    public Texture2D? Texture { get; set; }
}

public partial class ColorRect : Control
{
    public Color Color { get; set; }
}

public partial class Panel : Control { }
public partial class PanelContainer : Control { }

public partial class Container : Control { }
public partial class BoxContainer : Container { }
public partial class VBoxContainer : BoxContainer { }
public partial class HBoxContainer : BoxContainer { }
public partial class FlowContainer : Container { }
public partial class HFlowContainer : FlowContainer { }
public partial class GridContainer : Container
{
    public int Columns { get; set; }
}
public partial class MarginContainer : Container { }
public partial class CenterContainer : Container { }
public partial class ScrollContainer : Container { }
public partial class SubViewportContainer : Container { }
public partial class SubViewport : Viewport { }

public partial class Label : Control
{
    public string Text { get; set; } = "";
}

public partial class RichTextLabel : Control
{
    public string Text { get; set; } = "";
    public void Clear() { Text = ""; }
    public void AppendText(string text) { Text += text; }
    public void AddText(string text) { Text += text; }
}

public partial class Button : BaseButton
{
    public new partial class SignalName : Control.SignalName
    {
        public static readonly StringName Pressed = "Pressed";
    }
    public string Text { get; set; } = "";
    public event Action? Pressed;
}

public partial class BaseButton : Control
{
    public new partial class SignalName : Control.SignalName
    {
        public static readonly StringName Pressed = "Pressed";
    }
}

public partial class CheckBox : Button { }
public partial class CheckButton : Button { }

public partial class OptionButton : Button
{
    public int Selected { get; set; }
    public void AddItem(string label, int id = -1) { }
    public void Select(int idx) { Selected = idx; }
}

public partial class LineEdit : Control
{
    public string Text { get; set; } = "";
    public string PlaceholderText { get; set; } = "";
    public new partial class SignalName : Control.SignalName
    {
        public static readonly StringName TextChanged = "TextChanged";
        public static readonly StringName TextSubmitted = "TextSubmitted";
    }
}

public partial class TextEdit : Control
{
    public string Text { get; set; } = "";
}

public partial class SpinBox : Control
{
    public double Value { get; set; }
}

public partial class Slider : Control
{
    public double Value { get; set; }
}
public partial class HSlider : Slider { }
public partial class VSlider : Slider { }

public partial class ScrollBar : Control
{
    public double Value { get; set; }
}
public partial class HScrollBar : ScrollBar { }
public partial class VScrollBar : ScrollBar { }

public partial class Separator : Control { }
public partial class HSeparator : Separator { }
public partial class VSeparator : Separator { }

public partial class TabContainer : Container { }

// Timer
public partial class Timer : Node
{
    public double WaitTime { get; set; } = 1.0;
    public bool OneShot { get; set; }
    public bool Autostart { get; set; }
    public event Action? Timeout;
    public void Start(double timeSec = -1) { Timeout?.Invoke(); }
    public void Stop() { }
}

// Audio
public partial class AudioStream : Resource { }
public partial class AudioStreamPlayer : Node
{
    public new partial class SignalName : Node.SignalName
    {
        public static readonly StringName Finished = "Finished";
    }
    public AudioStream? Stream { get; set; }
    public float VolumeDb { get; set; }
    public void Play(float fromPosition = 0) { }
    public void Stop() { }
}

// Input
public partial class InputEvent : Resource
{
    public virtual bool IsActionPressed(StringName action, bool allowEcho = false) => false;
    public virtual bool IsActionReleased(StringName action) => false;
    public bool IsPressed() => false;
    public bool IsReleased() => true;
}
public partial class InputEventKey : InputEventWithModifiers { }
public partial class InputEventMouseButton : InputEventMouse
{
    public Vector2 Position { get; set; }
    public Vector2 GlobalPosition { get; set; }
}
public partial class InputEventMouseMotion : InputEventMouse
{
    public Vector2 Position { get; set; }
    public Vector2 Relative { get; set; }
}

// FileAccess
public partial class FileAccess : GodotObject, IDisposable
{
    public enum ModeFlags : long { Read = 1, Write = 2, ReadWrite = 3, WriteRead = 7 }
    public static FileAccess? Open(string path, ModeFlags flags) => null;
    public static bool FileExists(string path) => File.Exists(path);
    public string GetAsText(bool skipCr = false) => "";
    public bool StoreString(string str) => true;
    public void Close() { }
    public void Dispose() { }
}

// DirAccess
public partial class DirAccess : GodotObject, IDisposable
{
    public static bool DirExistsAbsolute(string path) => Directory.Exists(path);
    public static Error MakeDirAbsolute(string path) { try { Directory.CreateDirectory(path); return Error.Ok; } catch { return Error.Failed; } }
    public static Error MakeDirRecursiveAbsolute(string path) { try { Directory.CreateDirectory(path); return Error.Ok; } catch { return Error.Failed; } }
    public Error MakeDirRecursive(string path) { try { Directory.CreateDirectory(Path.Combine(_path, path)); return Error.Ok; } catch { return Error.Failed; } }
    public static DirAccess? Open(string path) => Directory.Exists(path) ? new DirAccess(path) : null;
    private readonly string _path;
    private DirAccess(string path) { _path = path; }
    public DirAccess() { _path = ""; }
    public string[] GetFiles() { try { return Directory.GetFiles(_path).Select(Path.GetFileName).ToArray()!; } catch { return Array.Empty<string>(); } }
    public string[] GetDirectories() { try { return Directory.GetDirectories(_path).Select(Path.GetFileName).ToArray()!; } catch { return Array.Empty<string>(); } }
    public void Dispose() { }
}

// Animation
public partial class AnimationPlayer : AnimationMixer
{
    public new partial class SignalName : Node.SignalName
    {
        public static readonly StringName AnimationFinished = "AnimationFinished";
    }
    public void Play(StringName name = default, double customBlend = -1, float customSpeed = 1f, bool fromEnd = false) { }
    public void Stop(bool keepState = false) { }
}

// Particles — no-op stubs prevent headless crashes (KinPriest VFX, etc.)
public partial class GpuParticles2D : Node2D
{
    public int Amount { get; set; }
    public bool Emitting { get; set; }
    public double Lifetime { get; set; }
    public float LifetimeRandomness { get; set; }
    public bool OneShot { get; set; }
    public bool LocalCoords { get; set; }
    public float SpeedScale { get; set; }
    public float Explosiveness { get; set; }
    public Material? ProcessMaterial { get; set; }
    public void Restart() { }
    // build 23372702 sets Texture on enemy/VFX particles (BygoneEffigy); the
    // other members upstream added here are already declared above.
    public Texture2D? Texture { get; set; }
}

// Sprite
public partial class Sprite2D : Node2D
{
    public Texture2D? Texture { get; set; }
}

// CharFXTransform for RichTextEffects
public partial class CharFXTransform : GodotObject
{
    public Color Color { get; set; } = Color.White;
    public Vector2 Offset { get; set; }
    public Transform2D Transform { get; set; }
    public bool Visible { get; set; } = true;
    public double ElapsedTime { get; set; }
    public int RelativeIndex { get; set; }
    public Godot.Collections.Dictionary? Env { get; set; }
}

// RichTextEffect
public partial class RichTextEffect : Resource
{
    public new partial class SignalName { }
    public virtual bool _ProcessCustomFX(CharFXTransform charFx) => false;
}

// ResourceFormatLoader
public partial class ResourceFormatLoader : GodotObject
{
    public partial class MethodName { }
    public partial class PropertyName { }
    public partial class SignalName { }

    public virtual Variant _Load(string path, string originalPath, bool useSubThreads, int cacheMode) => default;
    public virtual string[] _GetRecognizedExtensions() => Array.Empty<string>();
    public virtual bool _HandlesType(StringName type) => false;
    public virtual string _GetResourceType(string path) => "";
    public virtual bool _RecognizePath(string path, StringName type) => false;
    public virtual string[] _GetDependencies(string path, bool addTypes) => Array.Empty<string>();
    public virtual bool _Exists(string path) => false;
}

// Image
public partial class Image : Resource
{
    public enum Format : long { Rgba8 = 5 }
    public static Image CreateEmpty(int width, int height, bool useMipmaps, Format format) => new();
    public void SetPixel(int x, int y, Color color) { }
}
