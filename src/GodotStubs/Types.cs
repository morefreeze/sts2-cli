using System.Runtime.CompilerServices;

namespace Godot;

// StringName - wraps string (must be a class, not struct, to match real Godot's type signature)
public sealed partial class StringName : IDisposable
{
    private readonly string? _name;
    public StringName() => _name = "";
    public StringName(string name) => _name = name;
    public static implicit operator StringName(string s) => new(s);
    public static implicit operator string(StringName s) => s?._name ?? "";
    public override string ToString() => _name ?? "";
    public override int GetHashCode() => (_name ?? "").GetHashCode();
    public override bool Equals(object? obj) => obj switch
    {
        StringName sn => _name == sn._name,
        string s => _name == s,
        _ => false
    };
    public static bool operator ==(StringName? a, StringName? b) => (a?._name ?? "") == (b?._name ?? "");
    public static bool operator !=(StringName? a, StringName? b) => !(a == b);
    public void Dispose() { }
}

// NodePath (must be a class to match real Godot's type signature)
public sealed partial class NodePath : IDisposable
{
    private readonly string _path;
    public NodePath() => _path = "";
    public NodePath(string path) => _path = path;
    public static implicit operator NodePath(string s) => new(s);
    public static implicit operator string(NodePath p) => p?._path ?? "";
    public override string ToString() => _path ?? "";
    public void Dispose() { }
}

// Variant - can hold any type
public partial struct Variant
{
    // Numbering is real Godot's: sts2.dll compares against the constants compiled from the real enum.
    public enum Type : long
    {
        Nil = 0,
        Bool = 1,
        Int = 2,
        Float = 3,
        String = 4,
        Vector2 = 5,
        Vector2I = 6,
        Rect2 = 7,
        Rect2I = 8,
        Vector3 = 9,
        Vector3I = 10,
        Transform2D = 11,
        Vector4 = 12,
        Vector4I = 13,
        Plane = 14,
        Quaternion = 15,
        Aabb = 16,
        Basis = 17,
        Transform3D = 18,
        Projection = 19,
        Color = 20,
        StringName = 21,
        NodePath = 22,
        Rid = 23,
        Object = 24,
        Callable = 25,
        Signal = 26,
        Dictionary = 27,
        Array = 28,
        PackedByteArray = 29,
        PackedInt32Array = 30,
        PackedInt64Array = 31,
        PackedFloat32Array = 32,
        PackedFloat64Array = 33,
        PackedStringArray = 34,
        PackedVector2Array = 35,
        PackedVector3Array = 36,
        PackedColorArray = 37,
        PackedVector4Array = 38,
        Max = 39
    }

    private readonly object? _value;
    public Variant(object? value) => _value = value;

    public static Variant From<T>(T value) => new(value);
    public static Variant CreateFrom<T>(T value) => new(value);

    public T As<T>() => (T)_value!;
    public object? Obj => _value;

    public static implicit operator Variant(bool v) => new(v);
    public static implicit operator Variant(int v) => new(v);
    public static implicit operator Variant(long v) => new(v);
    public static implicit operator Variant(float v) => new(v);
    public static implicit operator Variant(double v) => new(v);
    public static implicit operator Variant(string v) => new(v);
    public static implicit operator Variant(StringName v) => new(v);
    public static implicit operator Variant(GodotObject v) => new(v);
    public static implicit operator Variant(Vector2 v) => new(v);
    public static implicit operator Variant(Color v) => new(v);

    public static explicit operator bool(Variant v) => v._value is bool b ? b : false;
    public static implicit operator int(Variant v) => v._value is int i ? i : 0;
    public static explicit operator long(Variant v) => v._value is long l ? l : 0;
    public static explicit operator float(Variant v) => v._value is float f ? f : 0f;
    public static implicit operator double(Variant v) => v._value is double d ? d : 0;
    public static implicit operator string(Variant v) => v._value?.ToString() ?? "";
}

// Callable - wraps a delegate
public partial struct Callable
{
    private readonly Delegate? _delegate;
    public Callable(Delegate? d) => _delegate = d;

    public static Callable From(Action action) => new(action);
    public static Callable From<T>(Action<T> action) => new(action);
    public static Callable From<T1, T2>(Action<T1, T2> action) => new(action);

    // Real Godot: Variant Call(params Variant[]); sts2.dll binds to the return type.
    public Variant Call(params Variant[] args) { (_delegate as Action)?.Invoke(); return default; }
    public void CallDeferred(params Variant[] args) => Call(args);
}

// Signal-related
[AttributeUsage(AttributeTargets.Delegate)]
public partial class SignalAttribute : Attribute { }

// IAwaiter - interface referenced by sts2.dll for async/await patterns
public partial interface IAwaiter : INotifyCompletion
{
    bool IsCompleted { get; }
    void GetResult();
}

public partial interface IAwaiter<out T> : INotifyCompletion
{
    bool IsCompleted { get; }
    T GetResult();
}

// SignalAwaiter - awaitable (must implement IAwaiter<Variant[]> to match real Godot)
public partial class SignalAwaiter : IAwaiter<Variant[]>
{
    private bool _completed = true;
    private Action? _continuation;

    public IAwaiter<Variant[]> GetAwaiter() => this;
    public bool IsCompleted => _completed;
    public Variant[] GetResult() => Array.Empty<Variant>();
    public void OnCompleted(Action continuation)
    {
        if (_completed)
            continuation();
        else
            _continuation = continuation;
    }

    internal void Complete()
    {
        _completed = true;
        _continuation?.Invoke();
    }
}

// Export attribute
public enum PropertyHint : long
{
    None = 0,
    Range = 1,
    Enum = 2,
    ResourceType = 17,
    NodeType = 34,
    TypeString = 23,
    File = 13,
    Dir = 14,
    GlobalFile = 15,
    GlobalDir = 16
}

[Flags]
public enum PropertyUsageFlags : long
{
    None = 0,
    Default = 6,
    ScriptVariable = 4096,
    Storage = 2,
    Editor = 4
}

public enum MethodFlags : long { Normal = 1, Editor = 2, Virtual = 8 }

[AttributeUsage(AttributeTargets.Property | AttributeTargets.Field)]
public partial class ExportAttribute : Attribute
{
    public ExportAttribute() { }
    public ExportAttribute(PropertyHint hint, string hintString = "") { }
}

[AttributeUsage(AttributeTargets.Property | AttributeTargets.Field)]
public partial class ExportGroupAttribute : Attribute
{
    public ExportGroupAttribute(string name, string prefix = "") { }
}

[AttributeUsage(AttributeTargets.Property | AttributeTargets.Field)]
public partial class ExportCategoryAttribute : Attribute
{
    public ExportCategoryAttribute(string name) { }
}

[AttributeUsage(AttributeTargets.Class)]
public partial class ScriptPathAttribute : Attribute
{
    public ScriptPathAttribute(string path) { }
}

[AttributeUsage(AttributeTargets.Class)]
public partial class GlobalClassAttribute : Attribute { }

// PropertyInfo for Godot bridge methods
public partial struct PropertyInfo
{
    public Variant.Type Type;
    public StringName Name;
    public PropertyHint Hint;
    public string HintString;
    public PropertyUsageFlags Usage;
    public bool Exported;

    public PropertyInfo(Variant.Type type, StringName name, PropertyHint hint = PropertyHint.None,
        string hintString = "", PropertyUsageFlags usage = PropertyUsageFlags.Default, bool exported = false)
    {
        Type = type; Name = name; Hint = hint; HintString = hintString; Usage = usage; Exported = exported;
    }
}

// MethodInfo for Godot bridge methods
public partial struct MethodInfo
{
    public StringName Name;
    public PropertyInfo ReturnVal;
    public MethodFlags Flags;
    public List<PropertyInfo>? DefaultArguments;
    public List<PropertyInfo>? Arguments;

    public MethodInfo(StringName name, PropertyInfo returnVal, MethodFlags flags,
        List<PropertyInfo>? arguments, List<PropertyInfo>? defaultArguments)
    {
        Name = name; ReturnVal = returnVal; Flags = flags;
        Arguments = arguments; DefaultArguments = defaultArguments;
    }
}

// GodotSerializationInfo
public partial class GodotSerializationInfo
{
    public void AddProperty(string name, Variant value) { }
    public bool TryGetProperty(string name, out Variant value) { value = default; return false; }
}
