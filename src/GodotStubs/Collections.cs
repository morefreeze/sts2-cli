namespace Godot.Collections;

// Godot Array<T> wrapper
public partial class Array<T> : List<T>
{
    public Array() { }
    public Array(IEnumerable<T> items) : base(items) { }

    // Real Godot's Array<T>.GetEnumerator() returns the interface (sts2.dll binds to that signature).
    public new IEnumerator<T> GetEnumerator() => ((IEnumerable<T>)(List<T>)this).GetEnumerator();
    public static explicit operator Array<T>(Variant from) => from.Obj as Array<T> ?? new Array<T>();
    public static implicit operator Variant(Array<T> from) => new Variant(from);
}

// Godot Dictionary
public partial class Dictionary<TKey, TValue> : System.Collections.Generic.Dictionary<TKey, TValue>
    where TKey : notnull
{
    public Dictionary() { }
}

// Non-generic Array
public partial class Array : List<Variant>
{
    public Array() { }
}

// Non-generic Dictionary (Variant -> Variant), the type Godot hands out for untyped dictionaries.
public partial class Dictionary : System.Collections.Generic.Dictionary<Variant, Variant>
{
    public Dictionary() { }

    public new IEnumerator<KeyValuePair<Variant, Variant>> GetEnumerator() =>
        ((IEnumerable<KeyValuePair<Variant, Variant>>)(System.Collections.Generic.Dictionary<Variant, Variant>)this).GetEnumerator();
}
