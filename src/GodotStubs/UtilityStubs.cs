// Working implementations (not no-ops) of Godot API that sts2.dll uses and the stubs lacked
// (found by tools/audit_godot_stub_refs.py, BUG-052).
//
// The bulk of the missing API is UI / rendering / input and is covered by the generated no-ops in
// GeneratedAuditStubs.cs. This file is for the members where "do nothing, return default" would
// be silently WRONG instead of loudly missing: math and value-type operators (Mathf.Max -> 0),
// property/method pairs that must agree (SetVisible vs Visible: Crusher.AfterAddedToRoom calls
// SetVisible), the scene-tree child list the stub already models, collections, and values the
// game branches on (Engine.TimeScale defaulting to 0, DisplayServer.GetName() == "headless").
//
// Every signature is the real Godot 4.5 one. All stub types are `partial`, so this adds members
// to the hand-written declarations in the other files without editing them.
using System.Runtime.CompilerServices;

namespace Godot;

public static partial class Mathf
{
    public static int Abs(int s) => Math.Abs(s);
    public static float Atan2(float y, float x) => MathF.Atan2(y, x);
    public static double DegToRad(double deg) => deg * (Math.PI / 180.0);
    public static float InverseLerp(float from, float to, float weight) => (weight - from) / (to - from);
    public static double Lerp(double from, double to, double weight) => from + (to - from) * weight;
    public static float LerpAngle(float from, float to, float weight)
    {
        float difference = (to - from) % Tau;
        float distance = ((2 * difference) % Tau) - difference;
        return from + distance * weight;
    }
    public static float LinearToDb(float linear) => MathF.Log(linear) * 8.6858896380650365530225783783321f;
    public static float Log(float s) => MathF.Log(s);
    public static double Max(double a, double b) => Math.Max(a, b);
    public static int Sign(int s) => Math.Sign(s);
    public static double Sin(double s) => Math.Sin(s);
    public static float Wrap(float value, float min, float max)
    {
        float range = max - min;
        if (MathF.Abs(range) < Epsilon) return min;
        return min + ((((value - min) % range) + range) % range);
    }
}

public partial struct Vector2
{
    public float Angle() => MathF.Atan2(Y, X);
    public float DistanceSquaredTo(Vector2 to) => (to - this).LengthSquared();
    public Vector2 Inverse() => new(1 / X, 1 / Y);
    public bool IsEqualApprox(Vector2 other) => Mathf.IsEqualApprox(X, other.X) && Mathf.IsEqualApprox(Y, other.Y);
    public Vector2 LimitLength(float length = 1.0f)
    {
        float len = Length();
        return len > 0 && length < len ? this / len * length : this;
    }
    public Vector2 Rotated(float angle)
    {
        float sine = MathF.Sin(angle), cosi = MathF.Cos(angle);
        return new Vector2(X * cosi - Y * sine, X * sine + Y * cosi);
    }
}

public partial struct Vector2I : IEquatable<Vector2I>
{
    public static Vector2I One { get; } = new(1, 1);
    public float DistanceTo(Vector2I to) => MathF.Sqrt((to.X - X) * (to.X - X) + (to.Y - Y) * (to.Y - Y));
    public static Vector2I operator +(Vector2I left, Vector2I right) => new(left.X + right.X, left.Y + right.Y);
    public static Vector2I operator -(Vector2I left, Vector2I right) => new(left.X - right.X, left.Y - right.Y);
    public static Vector2I operator *(Vector2I vec, int scale) => new(vec.X * scale, vec.Y * scale);
    public static Vector2I operator /(Vector2I vec, int divisor) => new(vec.X / divisor, vec.Y / divisor);
    public static bool operator ==(Vector2I left, Vector2I right) => left.X == right.X && left.Y == right.Y;
    public static bool operator !=(Vector2I left, Vector2I right) => !(left == right);
    public static bool operator <(Vector2I left, Vector2I right) => left.X == right.X ? left.Y < right.Y : left.X < right.X;
    public static bool operator >(Vector2I left, Vector2I right) => left.X == right.X ? left.Y > right.Y : left.X > right.X;
    public static bool operator <=(Vector2I left, Vector2I right) => left.X == right.X ? left.Y <= right.Y : left.X < right.X;
    public static bool operator >=(Vector2I left, Vector2I right) => left.X == right.X ? left.Y >= right.Y : left.X > right.X;
    public static explicit operator Vector2I(Vector2 value) => new((int)value.X, (int)value.Y);
    public bool Equals(Vector2I other) => this == other;
    public override bool Equals(object? obj) => obj is Vector2I v && this == v;
    public override int GetHashCode() => HashCode.Combine(X, Y);
    public override string ToString() => $"({X}, {Y})";
}

public partial struct Vector3
{
    public static Vector3 Up { get; } = new(0, 1, 0);
    public static Vector3 operator +(Vector3 left, Vector3 right) => new(left.X + right.X, left.Y + right.Y, left.Z + right.Z);
    public static Vector3 operator *(Vector3 vec, float scale) => new(vec.X * scale, vec.Y * scale, vec.Z * scale);
    public Vector3 Cross(Vector3 with) => new(Y * with.Z - Z * with.Y, Z * with.X - X * with.Z, X * with.Y - Y * with.X);
}

public partial struct Quaternion
{
    public float X, Y, Z, W;
    public static Quaternion Identity { get; } = new(0, 0, 0, 1);
    public Quaternion(float x, float y, float z, float w) { X = x; Y = y; Z = z; W = w; }

    /// <summary>Godot's YXZ Euler order.</summary>
    public static Quaternion FromEuler(Vector3 eulerYXZ)
    {
        float halfA1 = eulerYXZ.Y * 0.5f, halfA2 = eulerYXZ.X * 0.5f, halfA3 = eulerYXZ.Z * 0.5f;
        float cosA1 = MathF.Cos(halfA1), sinA1 = MathF.Sin(halfA1);
        float cosA2 = MathF.Cos(halfA2), sinA2 = MathF.Sin(halfA2);
        float cosA3 = MathF.Cos(halfA3), sinA3 = MathF.Sin(halfA3);
        return new Quaternion(
            sinA1 * cosA2 * sinA3 + cosA1 * sinA2 * cosA3,
            sinA1 * cosA2 * cosA3 - cosA1 * sinA2 * sinA3,
            -sinA1 * sinA2 * cosA3 + cosA1 * cosA2 * sinA3,
            sinA1 * sinA2 * sinA3 + cosA1 * cosA2 * cosA3);
    }

    public static Vector3 operator *(Quaternion quaternion, Vector3 vector)
    {
        var u = new Vector3(quaternion.X, quaternion.Y, quaternion.Z);
        var uv = u.Cross(vector);
        return vector + ((uv * quaternion.W) + u.Cross(uv)) * 2f;
    }
}

public partial struct Color
{
    public static Color operator *(Color left, Color right) => new(left.R * right.R, left.G * right.G, left.B * right.B, left.A * right.A);

    /// <summary>#RGB, #RGBA, #RRGGBB or #RRGGBBAA (the leading # is optional). Invalid input gives opaque black.</summary>
    public static Color FromHtml(ReadOnlySpan<char> rgba)
    {
        string s = rgba.ToString().TrimStart('#');
        static float Hex(string str, int i, int len)
        {
            int v = int.Parse(str.AsSpan(i, len), System.Globalization.NumberStyles.HexNumber);
            return len == 1 ? v * 17 / 255f : v / 255f;
        }
        try
        {
            return s.Length switch
            {
                3 => new Color(Hex(s, 0, 1), Hex(s, 1, 1), Hex(s, 2, 1)),
                4 => new Color(Hex(s, 0, 1), Hex(s, 1, 1), Hex(s, 2, 1), Hex(s, 3, 1)),
                6 => new Color(Hex(s, 0, 2), Hex(s, 2, 2), Hex(s, 4, 2)),
                8 => new Color(Hex(s, 0, 2), Hex(s, 2, 2), Hex(s, 4, 2), Hex(s, 6, 2)),
                _ => new Color(0, 0, 0),
            };
        }
        catch (FormatException)
        {
            return new Color(0, 0, 0);
        }
    }
}

public static partial class Colors
{
    public static Color DarkRed { get; } = new(0.545098f, 0, 0);
    public static Color DimGray { get; } = new(0.411765f, 0.411765f, 0.411765f);
    public static Color Gold { get; } = new(1, 0.843137f, 0);
}

public partial struct Rect2
{
    public Vector2 End { get => _position + _size; set => _size = value - _position; }
    public Vector2 GetCenter() => _position + _size * 0.5f;
    /// <summary>Half-open like Godot: the left/top edge is inside, the right/bottom edge is not.</summary>
    public bool HasPoint(Vector2 point) =>
        point.X >= _position.X && point.Y >= _position.Y && point.X < _position.X + _size.X && point.Y < _position.Y + _size.Y;
    public bool Intersects(Rect2 b, bool includeBorders = false)
    {
        if (includeBorders)
            return !(_position.X > b._position.X + b._size.X || _position.X + _size.X < b._position.X ||
                     _position.Y > b._position.Y + b._size.Y || _position.Y + _size.Y < b._position.Y);
        return !(_position.X >= b._position.X + b._size.X || _position.X + _size.X <= b._position.X ||
                 _position.Y >= b._position.Y + b._size.Y || _position.Y + _size.Y <= b._position.Y);
    }
    public Rect2 Merge(Rect2 b)
    {
        var begin = new Vector2(MathF.Min(b._position.X, _position.X), MathF.Min(b._position.Y, _position.Y));
        var end = new Vector2(MathF.Max(b.End.X, End.X), MathF.Max(b.End.Y, End.Y));
        return new Rect2(begin, end - begin);
    }
}

public partial struct Transform2D
{
    /// <summary>Transform a vector by the basis only (no translation).</summary>
    public Vector2 BasisXform(Vector2 v) => new(X.X * v.X + Y.X * v.Y, X.Y * v.X + Y.Y * v.Y);
    public static Vector2 operator *(Transform2D transform, Vector2 vector) => transform.BasisXform(vector) + transform.Origin;
    public Transform2D Inverse()
    {
        var inv = this;
        (inv.X.Y, inv.Y.X) = (Y.X, X.Y);
        inv.Origin = inv.BasisXform(-Origin);
        return inv;
    }
    public Transform2D RotatedLocal(float angle)
    {
        float c = MathF.Cos(angle), s = MathF.Sin(angle);
        var r = this;
        r.X = BasisXform(new Vector2(c, s));
        r.Y = BasisXform(new Vector2(-s, c));
        return r;
    }
    public Transform2D TranslatedLocal(Vector2 offset)
    {
        var r = this;
        r.Origin += BasisXform(offset);
        return r;
    }
    public float Rotation
    {
        get => MathF.Atan2(X.Y, X.X);
        set
        {
            var scale = Scale;
            float c = MathF.Cos(value), s = MathF.Sin(value);
            X = new Vector2(c, s) * scale.X;
            Y = new Vector2(-s, c) * scale.Y;
        }
    }
    public Vector2 Scale
    {
        get
        {
            float det = X.X * Y.Y - X.Y * Y.X;
            return new Vector2(X.Length(), (det < 0 ? -1f : 1f) * Y.Length());
        }
        set
        {
            X = X.Normalized() * value.X;
            Y = Y.Normalized() * value.Y;
        }
    }
}

public partial struct Variant
{
    public bool AsBool() => _value is bool b && b;
    public double AsDouble() => _value switch { double d => d, float f => f, int i => i, long l => l, _ => 0.0 };
    public float AsSingle() => (float)AsDouble();
    public string AsString() => _value as string ?? _value?.ToString() ?? "";
    public StringName AsStringName() => _value as StringName ?? new StringName(AsString());
    public Vector2 AsVector2() => _value is Vector2 v ? v : default;
    public Callable AsCallable() => _value is Callable c ? c : default;
    public Signal AsSignal() => _value is Signal s ? s : default;
    public byte[] AsByteArray() => _value as byte[] ?? Array.Empty<byte>();
    public GodotObject AsGodotObject() => (_value as GodotObject)!;
    public Godot.Collections.Array<T> AsGodotArray<T>() => _value as Godot.Collections.Array<T> ?? new Godot.Collections.Array<T>();
    public Godot.Collections.Dictionary<TKey, TValue> AsGodotDictionary<TKey, TValue>() where TKey : notnull =>
        _value as Godot.Collections.Dictionary<TKey, TValue> ?? new Godot.Collections.Dictionary<TKey, TValue>();
    public T[] AsGodotObjectArray<T>() where T : GodotObject => _value as T[] ?? Array.Empty<T>();

    /// <summary>The kind of value held (the numbering of this stub's Type enum is real Godot's).</summary>
    public Type VariantType => _value switch
    {
        null => Type.Nil,
        bool => Type.Bool,
        int or long or uint or ulong or short or ushort or byte or sbyte => Type.Int,
        float or double => Type.Float,
        string => Type.String,
        Vector2 => Type.Vector2,
        Vector2I => Type.Vector2I,
        Rect2 => Type.Rect2,
        Vector3 => Type.Vector3,
        Transform2D => Type.Transform2D,
        Color => Type.Color,
        StringName => Type.StringName,
        NodePath => Type.NodePath,
        Callable => Type.Callable,
        Signal => Type.Signal,
        Godot.Collections.Dictionary => Type.Dictionary,
        GodotObject => Type.Object,
        System.Collections.IList => Type.Array,
        _ => Type.Nil,
    };

    public static Variant CreateFrom<T>(Godot.Collections.Array<T> from) => new(from);
    public static Variant CreateFrom<TKey, TValue>(Godot.Collections.Dictionary<TKey, TValue> from) where TKey : notnull => new(from);
    public static Variant CreateFrom(GodotObject[] from) => new(from);
    public static Variant CreateFrom(string from) => new(from);
    public static Variant From<T>(in T from) => new(from);

    public static explicit operator Color(Variant from) => from._value is Color c ? c : default;
    public static explicit operator Vector2(Variant from) => from._value is Vector2 v ? v : default;
    public static explicit operator ulong(Variant from) => from._value is IConvertible c ? Convert.ToUInt64(c) : 0UL;
    public static implicit operator Variant(Godot.Collections.Dictionary from) => new(from);
    public static implicit operator Variant(Vector3 from) => new(from);
}

/// <summary>A signal of an object (name + owner). Nothing is ever emitted in headless play.</summary>
public partial struct Signal
{
    public StringName Name { get; set; }
    public GodotObject? Owner { get; set; }
}

public partial struct Callable
{
    public static Callable From<T0, T1, T2>(Action<T0, T1, T2> action) => new(action);
    public static Callable From<T0, T1, T2, T3>(Action<T0, T1, T2, T3> action) => new(action);
    public static Callable From<TResult>(Func<TResult> func) => new(func);
    public Delegate? Delegate => _delegate;
    public StringName Method => new(_delegate?.Method.Name ?? "");
    public GodotObject? Target => _delegate?.Target as GodotObject;
}

public partial class GodotObject
{
    private static long _nextInstanceId;
    private static readonly ConditionalWeakTable<GodotObject, object> _instanceIds = new();

    /// <summary>A unique, stable id per object (code uses it as a dictionary key).</summary>
    public ulong GetInstanceId() => (ulong)(long)_instanceIds.GetValue(this, _ => (object)Interlocked.Increment(ref _nextInstanceId));
    public string GetClass() => GetType().Name;
}

public partial class Resource
{
    public Resource Duplicate(bool deep = false) => (Resource)MemberwiseClone();
}

public partial class Node
{
    public Node GetChild(int idx, bool includeInternal = false)
    {
        if (idx < 0) idx += _children.Count;
        return idx >= 0 && idx < _children.Count ? _children[idx] : null!;
    }
    public T GetChild<T>(int idx, bool includeInternal = false) where T : class => (GetChild(idx, includeInternal) as T)!;
    public T? GetChildOrNull<T>(int idx, bool includeInternal = false) where T : class => GetChild(idx, includeInternal) as T;
    public T GetParent<T>() where T : class => (_parent as T)!;
    public void MoveChild(Node childNode, int toIndex)
    {
        int from = _children.IndexOf(childNode);
        if (from < 0) return;
        _children.RemoveAt(from);
        if (toIndex < 0) toIndex += _children.Count + 1;
        _children.Insert(Math.Clamp(toIndex, 0, _children.Count), childNode);
    }
    public void AddSibling(Node sibling, bool forceReadableName = false) => _parent?.AddChild(sibling);
    public void SetName(string name) => Name = name;
    public bool IsNodeReady() => true;
    public bool HasNode(NodePath path) => false;
    // Like the generic GetNode<T>/GetNodeOrNull<T> above: there is no scene, so hand back a bare node.
    public Node GetNode(NodePath path) => new Node();
    public Node? GetNodeOrNull(NodePath path) => new Node();
    public Window GetWindow() => GetTree().Root;
    public NodePath GetPath()
    {
        var parts = new List<string>();
        for (Node? n = this; n != null; n = n._parent) parts.Add(n.Name.ToString());
        parts.Reverse();
        return new NodePath("/" + string.Join("/", parts));
    }
    public Node Duplicate(int flags = 15)
    {
        try { return (Node)Activator.CreateInstance(GetType())!; }
        catch (Exception) { return new Node(); }
    }
}

public partial class SceneTree
{
    public Tween CreateTween() => new Tween();
}

public partial class CanvasItem
{
    // The method forms must agree with the Visible property (Crusher.AfterAddedToRoom -> SetVisible).
    public void SetVisible(bool visible) => Visible = visible;
    public bool IsVisible() => Visible;
}

public partial class Control
{
    public Vector2 GetPosition() => Position;
    public void SetPosition(Vector2 position, bool keepOffsets = false) => Position = position;
    public void SetGlobalPosition(Vector2 position, bool keepOffsets = false) => GlobalPosition = position;
    public void SetSize(Vector2 size, bool keepOffsets = false) => Size = size;
    public Vector2 GetPivotOffset() => PivotOffset;
    public void SetPivotOffset(Vector2 pivotOffset) => PivotOffset = pivotOffset;
    public void SetFocusMode(FocusModeEnum mode) => FocusMode = mode;
    public Rect2 GetRect() => new(Position, Size);
    public Rect2 GetGlobalRect() => new(GlobalPosition, Size);
}

public static partial class Engine
{
    private static double _timeScale = 1.0;   // real Godot's default; 0 would freeze or divide by zero
    public static double TimeScale { get => _timeScale; set => _timeScale = value; }
    public static void SetTimeScale(double timeScale) => _timeScale = timeScale;
    public static int MaxFps { get; set; }
    public static double GetFramesPerSecond() => 60.0;
    public static string GetArchitectureName() =>
        System.Runtime.InteropServices.RuntimeInformation.OSArchitecture.ToString().ToLowerInvariant();
    public static Godot.Collections.Dictionary GetVersionInfo()
    {
        var d = new Godot.Collections.Dictionary();
        d.Add("major", 4);
        d.Add("minor", 5);
        d.Add("patch", 1);
        d.Add("hex", 0x040501);
        d.Add("status", "stable");
        d.Add("build", "official");
        d.Add("string", "4.5.1-stable (official)");
        return d;
    }
}

public static partial class GD
{
    public static T Load<T>(string path) where T : class => ResourceLoader.Load<T>(path)!;
    public static double RandRange(double from, double to) => Randf_Range(from, to);
}

public static partial class OS
{
    public static int GetProcessorCount() => Environment.ProcessorCount;
    public static string GetEnvironment(string variable) => Environment.GetEnvironmentVariable(variable) ?? "";
    public static string GetLocaleLanguage() => "en";
}

public static partial class TranslationServer
{
    private static string _locale = "en";
    public static string GetLocale() => _locale;
    public static void SetLocale(string locale) => _locale = locale;
}

/// <summary>Real Godot's headless display server reports itself as "headless"; game code branches on it.</summary>
public static partial class DisplayServer
{
    public static string GetName() => "headless";
    public static int GetScreenCount() => 1;
    public static int GetPrimaryScreen() => 0;
    public static int ScreenGetDpi(int screen = -1) => 96;
    public static float ScreenGetRefreshRate(int screen = -1) => 60f;
    public static float ScreenGetScale(int screen = -1) => 1f;
    public static Vector2I ScreenGetSize(int screen = -1) => new(1920, 1080);
    public static Vector2I WindowGetSize(int windowId = 0) => new(1920, 1080);
}

public static partial class StringExtensions
{
    /// <summary>The part after the last / or \ (the whole string if there is none).</summary>
    public static string GetFile(this string instance)
    {
        int sep = Math.Max(instance.LastIndexOf('/'), instance.LastIndexOf('\\'));
        return sep < 0 ? instance : instance.Substring(sep + 1);
    }

    /// <summary>Everything before the last / or \, keeping a "res://" / "user://" / "/" prefix.</summary>
    public static string GetBaseDir(this string instance)
    {
        int basepos = instance.IndexOf("://", StringComparison.Ordinal);
        string prefix, dir;
        if (basepos != -1) { prefix = instance.Substring(0, basepos + 3); dir = instance.Substring(basepos + 3); }
        else if (instance.StartsWith('/')) { prefix = "/"; dir = instance.Substring(1); }
        else { prefix = ""; dir = instance; }
        int sep = Math.Max(dir.LastIndexOf('/'), dir.LastIndexOf('\\'));
        return sep < 0 ? prefix : prefix + dir.Substring(0, sep);
    }

    /// <summary>fooBar / foo_bar -> foo_bar.</summary>
    public static string ToSnakeCase(this string instance)
    {
        var sb = new System.Text.StringBuilder();
        for (int i = 0; i < instance.Length; i++)
        {
            char c = instance[i];
            if (char.IsUpper(c) && i > 0 && (char.IsLower(instance[i - 1]) || char.IsDigit(instance[i - 1]) ||
                                              (i + 1 < instance.Length && char.IsLower(instance[i + 1]) && char.IsUpper(instance[i - 1]))))
                sb.Append('_');
            sb.Append(char.ToLowerInvariant(c));
        }
        return sb.ToString();
    }

    /// <summary>foo_bar / fooBar -> "Foo Bar".</summary>
    public static string Capitalize(this string instance)
    {
        var words = instance.ToSnakeCase().Replace('_', ' ').Split(' ', StringSplitOptions.RemoveEmptyEntries);
        return string.Join(" ", words.Select(w => char.ToUpperInvariant(w[0]) + w.Substring(1)));
    }
}
