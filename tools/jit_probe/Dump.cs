using System.Reflection;
using System.Reflection.Emit;

/// <summary>Prints every GodotSharp member/type token the IL of a method resolves to.</summary>
static class Dump
{
    public static void Run(Assembly asm, string typeName, string methodName)
    {
        var ops = new Dictionary<short, OpCode>();
        foreach (var f in typeof(OpCodes).GetFields(BindingFlags.Public | BindingFlags.Static))
            if (f.GetValue(null) is OpCode oc) ops[oc.Value] = oc;
        var type = asm.GetType(typeName, true);
        const BindingFlags F = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static | BindingFlags.DeclaredOnly;
        foreach (var m in type.GetMethods(F).Where(x => x.Name == methodName))
        {
            var il = m.GetMethodBody().GetILAsByteArray();
            for (int i = 0; i < il.Length;)
            {
                OpCode oc = il[i] == 0xFE ? ops[(short)(0xFE00 | il[i + 1])] : ops[il[i]];
                i += oc.Size;
                int size = oc.OperandType switch
                {
                    OperandType.InlineNone => 0,
                    OperandType.ShortInlineBrTarget or OperandType.ShortInlineI or OperandType.ShortInlineVar => 1,
                    OperandType.InlineVar => 2,
                    OperandType.InlineI8 or OperandType.InlineR => 8,
                    OperandType.InlineSwitch => 4 + 4 * BitConverter.ToInt32(il, i),
                    _ => 4,
                };
                if (oc.OperandType is OperandType.InlineMethod or OperandType.InlineField or OperandType.InlineType or OperandType.InlineTok)
                {
                    int token = BitConverter.ToInt32(il, i);
                    try
                    {
                        var member = m.Module.ResolveMember(token,
                            m.DeclaringType.IsGenericType ? m.DeclaringType.GetGenericArguments() : null,
                            m.IsGenericMethod ? m.GetGenericArguments() : null);
                        string assembly = (member as Type)?.Assembly.GetName().Name ?? member.DeclaringType?.Assembly.GetName().Name;
                        if (assembly == "GodotSharp") Console.WriteLine($"{oc.Name,-10} {member.MemberType} {member.DeclaringType}::{member}");
                    }
                    catch (Exception e) { Console.WriteLine($"{oc.Name,-10} UNRESOLVED {token:x8}: {e.GetType().Name} {e.Message.Split('\n')[0]}"); }
                }
                i += size;
            }
        }
    }
}
