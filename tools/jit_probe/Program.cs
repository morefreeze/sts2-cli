// JIT-compiles every non-generic method of sts2.dll against whichever GodotSharp.dll sits next to this
// program, WITHOUT running any game code, and prints the ones the runtime cannot compile
// (MissingMethodException, TypeLoadException, ...) -- the exact failure BUG-052 was.
// Driven by tools/jit_probe.py (which builds this into a temp dir; never `dotnet run` it in the repo).
//
//   Probe.dll                         -> "SUMMARY ..." then one "FAIL<TAB>exception<TAB>Type::Method" per failure
//   Probe.dll --dump <Type> <Method>  -> the Godot members that method's IL references
using System.Reflection;
using System.Runtime.CompilerServices;

var dir = AppContext.BaseDirectory;
AppDomain.CurrentDomain.AssemblyResolve += (_, a) =>
{
    var p = Path.Combine(dir, new AssemblyName(a.Name).Name + ".dll");
    return File.Exists(p) ? Assembly.LoadFrom(p) : null;
};
var asm = Assembly.LoadFrom(Path.Combine(dir, "sts2.dll"));
if (args.Length == 3 && args[0] == "--dump") { Dump.Run(asm, args[1], args[2]); return; }

Type[] types;
var fails = new List<(string key, string where)>();
try { types = asm.GetTypes(); }
catch (ReflectionTypeLoadException e)
{
    foreach (var le in e.LoaderExceptions.Where(x => x != null))
        fails.Add(("TYPELOAD " + le.GetType().Name + ": " + le.Message.Split('\n')[0], "(type load)"));
    types = e.Types.Where(t => t != null).ToArray();
}
int ok = 0, skipped = 0, total = 0;
const BindingFlags F = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static | BindingFlags.DeclaredOnly;
foreach (var t in types)
{
    if (t.ContainsGenericParameters) { skipped++; continue; }
    List<MethodBase> ms;
    try { ms = t.GetMethods(F).Cast<MethodBase>().Concat(t.GetConstructors(F)).ToList(); }
    catch (Exception e) { fails.Add(("REFLECT " + e.GetType().Name + ": " + e.Message.Split('\n')[0], t.FullName)); continue; }
    foreach (var m in ms)
    {
        if (m.IsAbstract || m.ContainsGenericParameters) { skipped++; continue; }
        total++;
        try { RuntimeHelpers.PrepareMethod(m.MethodHandle); ok++; }
        catch (Exception e)
        {
            var inner = e is TypeInitializationException { InnerException: { } ie } ? ie : e;
            fails.Add((inner.GetType().Name + ": " + inner.Message.Split('\n')[0], t.FullName + "::" + m.Name));
        }
    }
}
Console.WriteLine($"SUMMARY types={types.Length} tried={total} compiled={ok} skipped={skipped} failures={fails.Count}");
foreach (var (key, where) in fails) Console.WriteLine($"FAIL\t{key}\t{where}");
