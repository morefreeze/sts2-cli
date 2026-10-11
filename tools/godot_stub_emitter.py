"""Render no-op C# stubs for the holes found by tools/audit_godot_stub_refs.py.

The real GodotSharp.dll (shipped in the Steam game directory; read-only reference, never copied
into the repo) is the oracle: it says what kind of type a missing type is (class / struct / enum /
delegate / interface), its base class and generic parameters, and the exact modifiers, parameter
names, property/event grouping and enum values of each missing member.  Everything is emitted as
``partial`` declarations so that the hand-written stubs need no edits to receive new members
(``tools/audit_godot_stub_refs.py`` documents the one-off ``partial`` conversion).

Behaviour of what is emitted -- the same as every other stub: methods do nothing and return
``default``; properties are auto-properties (a value set is read back); events are plain events;
``SignalName``/``MethodName``/``PropertyName`` constants are ``StringName``s of the snake_case name.
Anything the generator cannot render safely (``ref`` returns, pointers, a hole that collides with a
hand-written member of the same name) becomes a ``// MANUAL`` comment and is counted, so a human
decides.
"""
from __future__ import annotations

import re
import struct
from collections import defaultdict

from clr_metadata import (Metadata, SigReader, TypeInfo, cs_name, generic_def, index_types,
                          params_key)

CS_KEYWORDS = {
    "abstract", "as", "base", "bool", "break", "byte", "case", "catch", "char", "checked", "class",
    "const", "continue", "decimal", "default", "delegate", "do", "double", "else", "enum", "event",
    "explicit", "extern", "false", "finally", "fixed", "float", "for", "foreach", "goto", "if",
    "implicit", "in", "int", "interface", "internal", "is", "lock", "long", "namespace", "new",
    "null", "object", "operator", "out", "override", "params", "private", "protected", "public",
    "readonly", "ref", "return", "sbyte", "sealed", "short", "sizeof", "stackalloc", "static",
    "string", "struct", "switch", "this", "throw", "true", "try", "typeof", "uint", "ulong",
    "unchecked", "unsafe", "ushort", "using", "virtual", "void", "volatile", "while",
}

OPERATORS = {
    "op_Addition": "+", "op_Subtraction": "-", "op_Multiply": "*", "op_Division": "/",
    "op_Modulus": "%", "op_BitwiseAnd": "&", "op_BitwiseOr": "|", "op_ExclusiveOr": "^",
    "op_LeftShift": "<<", "op_RightShift": ">>", "op_Equality": "==", "op_Inequality": "!=",
    "op_LessThan": "<", "op_GreaterThan": ">", "op_LessThanOrEqual": "<=",
    "op_GreaterThanOrEqual": ">=", "op_UnaryNegation": "-", "op_UnaryPlus": "+",
    "op_LogicalNot": "!", "op_OnesComplement": "~", "op_Increment": "++", "op_Decrement": "--",
}

OPERATOR_PAIRS = {"op_Equality": "op_Inequality", "op_Inequality": "op_Equality",
                  "op_LessThan": "op_GreaterThan", "op_GreaterThan": "op_LessThan",
                  "op_LessThanOrEqual": "op_GreaterThanOrEqual", "op_GreaterThanOrEqual": "op_LessThanOrEqual"}

NO_BASE = {"System.Object", "System.ValueType", "System.Enum", "System.MulticastDelegate"}


def esc(name):
    return "@" + name if name in CS_KEYWORDS else name


def simple_name(full):
    """'Godot.Collections.Array`1' -> 'Array'; 'Godot.Control/SignalName' -> 'SignalName'."""
    return full.split("/")[-1].rsplit(".", 1)[-1].split("`", 1)[0]


def snake(name):
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", name)
    return s.lower()


class RealMember:
    __slots__ = ("owner", "kind", "idx", "name", "flags", "ret", "params", "pnames", "pflags",
                 "ngen", "gnames", "sem")


class Oracle:
    """Index of the real GodotSharp.dll."""

    def __init__(self, path):
        self.path = path
        self.md = md = Metadata(path)
        self.types = index_types(md)
        td = md.table(0x02)
        self.mranges, self.franges = {}, {}
        for i, r in enumerate(td, start=1):
            f_end = td[i][4] if i < len(td) else len(md.table(0x04)) + 1
            m_end = td[i][5] if i < len(td) else len(md.table(0x06)) + 1
            name = md.typedef_name(i)
            self.franges[name] = (r[4], f_end)
            self.mranges[name] = (r[5], m_end)
        self.mgp = defaultdict(list)
        for num, _f, owner, name in md.table(0x2A):
            tbl, idx = md.decode_coded("TypeOrMethodDef", owner)
            if tbl == 0x06:
                self.mgp[idx].append((num, md.string(name)))
        self.sem = {}
        for sem, meth, assoc in md.table(0x18):
            tbl, idx = md.decode_coded("HasSemantics", assoc)
            self.sem[meth] = (sem, tbl, idx)
        self.prop_names = [md.string(r[1]) for r in md.table(0x17)]
        self.event_names = [md.string(r[1]) for r in md.table(0x14)]
        self.consts = {}
        for t, _pad, parent, val in md.table(0x0B):
            tbl, idx = md.decode_coded("HasConstant", parent)
            self.consts[(tbl, idx)] = (t, md.blob(val))
        self.flags_enums = set()
        for parent, ctype, _val in md.table(0x0C):
            ptbl, pidx = md.decode_coded("HasCustomAttribute", parent)
            if ptbl != 0x02:
                continue
            ctbl, cidx = md.decode_coded("CustomAttributeType", ctype)
            if ctbl == 0x0A:
                t2, i2 = md.decode_coded("MemberRefParent", md.row(0x0A, cidx)[0])
                if t2 == 0x01 and md.typeref_name(i2) == "System.FlagsAttribute":
                    self.flags_enums.add(pidx)
        self.children = defaultdict(list)
        for n, e in md.table(0x29):
            self.children[md.typedef_name(e)].append(md.typedef_name(n))
        self._mcache = {}

    # ---- lookup ----------------------------------------------------------------------------
    def _members(self, tname):
        c = self._mcache.get(tname)
        if c is None:
            md = self.md
            c = {"m": {}, "f": {}}
            m0, m1 = self.mranges[tname]
            for mi in range(m0, m1):
                r = md.table(0x06)[mi - 1]
                c["m"][(md.string(r[3]), SigReader(md, md.blob(r[4])).signature()[1])] = mi
            f0, f1 = self.franges[tname]
            for fi in range(f0, f1):
                r = md.table(0x04)[fi - 1]
                c["f"][(md.string(r[1]), SigReader(md, md.blob(r[2])).signature()[1])] = fi
            self._mcache[tname] = c
        return c

    def chain(self, name):
        out, n = [], generic_def(name)
        while n and n in self.types and n not in out:
            out.append(n)
            b = self.types[n].base
            n = generic_def(b) if b else None
        return out

    def find(self, type_name, member, sig, is_field):
        """Walk the real chain from `type_name` -> (declaring type, 'm'|'f', row idx) or None."""
        for t in self.chain(type_name):
            c = self._members(t)
            key = (member, sig)
            if is_field and key in c["f"]:
                return t, "f", c["f"][key]
            if not is_field and key in c["m"]:
                return t, "m", c["m"][key]
            if member in (".ctor", ".cctor"):
                break
        return None

    # ---- member details --------------------------------------------------------------------
    def type_gparams(self, tname):
        return self.types[tname].gparams

    def method(self, owner, midx):
        md = self.md
        r = md.table(0x06)[midx - 1]
        _rva, _impl, flags, name_i, sig_i, plist = r
        mgnames = [n for _, n in sorted(self.mgp.get(midx, []))]
        cc, ngen, ret, params = SigReader(md, md.blob(sig_i), cs=True,
                                          tparams=self.type_gparams(owner), mparams=mgnames).method_parts()
        p_end = md.table(0x06)[midx][5] if midx < len(md.table(0x06)) else len(md.table(0x08)) + 1
        pnames, pflags = {}, {}
        for pi in range(plist, p_end):
            pf, seq, pname = md.table(0x08)[pi - 1]
            pnames[seq], pflags[seq] = md.string(pname), pf
        m = RealMember()
        m.owner, m.kind, m.idx, m.name, m.flags = owner, "m", midx, md.string(name_i), flags
        m.ret, m.params, m.ngen, m.gnames = ret, params, ngen, mgnames
        m.pnames, m.pflags = pnames, pflags
        m.sem = self.sem.get(midx)
        return m

    def field(self, owner, fidx):
        md = self.md
        r = md.table(0x04)[fidx - 1]
        flags, name_i, sig_i = r
        typ = SigReader(md, md.blob(sig_i), cs=True, tparams=self.type_gparams(owner)).signature()[1]
        m = RealMember()
        m.owner, m.kind, m.idx, m.name, m.flags, m.ret = owner, "f", fidx, md.string(name_i), flags, typ
        return m

    def enum_underlying(self, tname):
        f0, f1 = self.franges[tname]
        for fi in range(f0, f1):
            r = self.md.table(0x04)[fi - 1]
            if not r[0] & 0x10 and self.md.string(r[1]) == "value__":
                return SigReader(self.md, self.md.blob(r[2]), cs=True).signature()[1]
        return "int"

    def enum_members(self, tname):
        out = []
        f0, f1 = self.franges[tname]
        for fi in range(f0, f1):
            flags, name_i, _sig = self.md.table(0x04)[fi - 1]
            if not flags & 0x40:  # literal
                continue
            c = self.consts.get((0x04, fi))
            if c is None:
                continue
            t, blob = c
            fmt = {0x04: "<b", 0x05: "<B", 0x06: "<h", 0x07: "<H", 0x08: "<i", 0x09: "<I",
                   0x0A: "<q", 0x0B: "<Q"}.get(t)
            if fmt:
                out.append((self.md.string(name_i), struct.unpack(fmt, blob[:struct.calcsize(fmt)])[0]))
        return out

    def invoke_signature(self, tname):
        """Delegate type -> (return type, [(name, type)])."""
        m0, m1 = self.mranges[tname]
        for mi in range(m0, m1):
            if self.md.string(self.md.table(0x06)[mi - 1][3]) == "Invoke":
                m = self.method(tname, mi)
                return m.ret, [(m.pnames.get(i + 1, f"arg{i}"), t) for i, t in enumerate(m.params)]
        return "void", []

    def const_value(self, fidx):
        c = self.consts.get((0x04, fidx))
        if c is None:
            return None
        t, blob = c
        fmt = {0x02: "<?", 0x04: "<b", 0x05: "<B", 0x06: "<h", 0x07: "<H", 0x08: "<i", 0x09: "<I",
               0x0A: "<q", 0x0B: "<Q", 0x0C: "<f", 0x0D: "<d"}.get(t)
        if fmt:
            return repr(struct.unpack(fmt, blob[:struct.calcsize(fmt)])[0])
        if t == 0x0E:
            return '"' + blob.decode("utf-16-le").replace("\\", "\\\\").replace('"', '\\"') + '"'
        return None


# ------------------------------------------------------------------------------------------
# Emission
# ------------------------------------------------------------------------------------------

class Emitter:
    def __init__(self, stubs, oracle):
        self.stubs = stubs            # name -> TypeInfo of the compiled stub assembly
        self.oracle = oracle
        self.gen = {}                 # generated (new) type name -> real TypeInfo
        self.members = defaultdict(list)   # target type -> [(kind, RealMember, hole-sig)]
        self.manual = []              # [(type, member, sig, reason)]
        self.unresolved = []          # holes the real Godot does not have either (tool bug / other assembly)
        self.emitted_members = 0

    # ---- planning --------------------------------------------------------------------------
    def chain(self, name):
        """Combined (stub + to-be-generated) base chain of a type, by name."""
        out, n = [], generic_def(name)
        while n and n not in out:
            out.append(n)
            if n in self.stubs:
                b = self.stubs[n].base
            elif n in self.gen:
                b = self.gen[n].base
            else:
                break
            n = generic_def(b) if b else None
        return out

    def ensure_type(self, name):
        name = generic_def(name)
        if name in self.stubs or name in self.gen:
            return True
        if name not in self.oracle.types:
            return False
        self.gen[name] = self.oracle.types[name]
        base = self.oracle.types[name].base
        if base and generic_def(base) in self.oracle.types:
            self.ensure_type(base)
        if "/" in name:
            self.ensure_type(name.rsplit("/", 1)[0])
        return True

    def add_holes(self, findings):
        for f in findings:
            if f.category == "type":
                if not self.ensure_type(f.type_name):
                    self.unresolved.append(f)
        for f in findings:
            if f.category != "member":
                continue
            if not self.ensure_type(f.type_name):
                self.unresolved.append(f)
                continue
            is_field = f.sig.startswith("field ")
            sig = f.sig[len("field "):] if is_field else f.sig
            loc = self.oracle.find(f.type_name, f.member, sig, is_field)
            if loc is None:
                self.unresolved.append(f)
                continue
            owner, kind, idx = loc
            target = owner if (owner in self.chain(f.type_name) and (owner in self.stubs or owner in self.gen)) else f.type_name
            if kind == "m":
                self.members[target].append(("m", self.oracle.method(owner, idx), sig))
            else:
                self.members[target].append(("f", self.oracle.field(owner, idx), sig))

    # ---- rendering helpers -----------------------------------------------------------------
    @staticmethod
    def default_expr(t):
        """What a no-op returns for C# type `t`: what real Godot returns for 'nothing', not a null."""
        if t == "string":
            return '""'
        if t.endswith("[]") and "," not in t:
            return f"global::System.Array.Empty<{t[:-2]}>()"
        if t in ("global::Godot.StringName", "global::Godot.NodePath"):
            return "new()"
        if t.startswith(("global::Godot.Collections.Array", "global::Godot.Collections.Dictionary")):
            return "new()"
        if t == "global::Godot.Error":
            return "global::Godot.Error.Failed"   # an operation that did nothing must not claim success
        return "default"

    @staticmethod
    def access(flags):
        return {6: "public", 4: "protected", 5: "protected internal"}.get(flags & 7, "public")

    def render_params(self, m):
        out, assigns = [], []
        for i, t in enumerate(m.params):
            name = esc(m.pnames.get(i + 1, f"p{i}"))
            pf = m.pflags.get(i + 1, 0)
            if t.startswith("ref "):
                t = t[4:]
                mod = "out " if pf & 2 and not pf & 1 else "in " if pf & 1 and not pf & 2 else "ref "
                if mod == "out ":
                    assigns.append(f"{name} = {self.default_expr(t)};")
            else:
                mod = ""
            out.append(f"{mod}{t} {name}")
        return ", ".join(out), assigns

    def render_method(self, target, m, in_interface, is_struct):
        owner_name = simple_name(target)
        ret = m.ret
        if ret.startswith("ref ") or "*" in ret or any("*" in p or p == "..." for p in m.params):
            return None, "ref-return / pointer / vararg signature"
        params, assigns = self.render_params(m)
        static = bool(m.flags & 0x10)
        mods = [] if in_interface else [self.access(m.flags)]
        if static:
            mods.append("static")
        elif m.flags & 0x40 and not in_interface and not is_struct and m.name != ".ctor":
            mods.append("virtual")
        gen = f"<{', '.join(esc(g) for g in m.gnames)}>" if m.ngen else ""
        if m.name in (".ctor", ".cctor"):
            if m.name == ".cctor":
                return None, "static constructor"
            return f"{' '.join(mods)} {owner_name}({params}) {{ }}", None
        if m.name in OPERATORS or m.name in ("op_Implicit", "op_Explicit"):
            if in_interface:
                return None, "operator on interface"
            sig_params = params
            if m.name in ("op_Implicit", "op_Explicit"):
                kw = "implicit" if m.name == "op_Implicit" else "explicit"
                return (f"public static {kw} operator {ret}({sig_params}) {{ {' '.join(assigns)} return {self.default_expr(ret)}; }}", None)
            return (f"public static {ret} operator {OPERATORS[m.name]}({sig_params}) "
                    f"{{ {' '.join(assigns)} return {self.default_expr(ret)}; }}", None)
        head = f"{' '.join(mods)} {ret} {esc(m.name)}{gen}({params})".strip()
        if in_interface:
            return head + ";", None
        body = "{ " + " ".join(assigns) + (" " if assigns else "")
        body += ("}" if ret == "void" else f"return {self.default_expr(ret)}; }}")
        return f"{head} {body}", None

    def render_field(self, m):
        mods = ["public"]
        if m.flags & 0x10:
            mods.append("static")
        if m.flags & 0x40:  # literal
            val = self.oracle.const_value(m.idx)
            if val is not None:
                return f"public const {m.ret} {esc(m.name)} = {val};", None
        if m.ret == "global::Godot.StringName" and m.flags & 0x10 and m.flags & 0x20:
            return f'public static readonly global::Godot.StringName {esc(m.name)} = "{snake(m.name)}";', None
        if m.flags & 0x20:
            mods.append("readonly")
        init = " = default" if (m.flags & 0x10 and m.flags & 0x20) else ""
        return f"{' '.join(mods)} {m.ret} {esc(m.name)}{init};", None

    # ---- per-type body ---------------------------------------------------------------------
    def type_body(self, target, exists, in_interface, is_struct):
        lines = []
        props = defaultdict(dict)    # (name, static) -> {'get': m, 'set': m}
        events = defaultdict(dict)
        stub = self.stubs.get(target)
        seen = set()
        have_ctors = {s for s in stub.methods.get(".ctor", ())} if stub else set()
        # an implicit default ctor exists only while the type declares no other ctor; adding one
        # removes it, so it must then be restated (unless the stub declares it explicitly)
        has_default_ctor = have_ctors == {"instance void()"}
        ctor_holes = set()
        for kind, m, sig in self.members.get(target, []):
            key = (kind, m.name, sig)
            if key in seen:
                continue
            seen.add(key)
            if kind == "f":
                text, why = self.render_field(m)
                if text is None:
                    self.manual.append((target, m.name, sig, why))
                    lines.append(f"// MANUAL: field {m.name} {sig}: {why}")
                elif stub and m.name in stub.fields:
                    self.manual.append((target, m.name, sig, "field exists with another type"))
                    lines.append(f"// MANUAL: field {m.name} already exists in the stub with another type: {text}")
                else:
                    lines.append(text)
                    self.emitted_members += 1
                continue
            if m.sem and m.sem[1] == 0x17:     # property accessor
                role = "get" if m.sem[0] & 2 else "set"
                props[(self.oracle.prop_names[m.sem[2] - 1], bool(m.flags & 0x10))][role] = m
                continue
            if m.sem and m.sem[1] == 0x14:     # event accessor
                role = "add" if m.sem[0] & 8 else "remove"
                events[(self.oracle.event_names[m.sem[2] - 1], bool(m.flags & 0x10))][role] = m
                continue
            if stub and m.name not in (".ctor", "op_Implicit", "op_Explicit"):  # conversions are keyed on the target too
                clash = [s for s in stub.methods.get(m.name, ()) if params_key(s) == params_key(sig)]
                if clash:
                    self.manual.append((target, m.name, sig, f"overload differs only by return type from {clash[0]}"))
                    lines.append(f"// MANUAL: {m.name} {sig}: same parameters as the stub's {clash[0]} -- change that member")
                    continue
            text, why = self.render_method(target, m, in_interface, is_struct)
            if m.name == ".ctor":
                ctor_holes.add(sig)
            if text is None:
                self.manual.append((target, m.name, sig, why))
                lines.append(f"// MANUAL: {m.name} {sig}: {why}")
            else:
                lines.append(text)
                self.emitted_members += 1
                if m.name in OPERATOR_PAIRS:   # C# requires == with !=, < with >, <= with >=
                    sym, other = OPERATORS[m.name], OPERATORS[OPERATOR_PAIRS[m.name]]
                    twin = text.replace(f"operator {sym}(", f"operator {other}(", 1)
                    held = any(k == "m" and mm.name == OPERATOR_PAIRS[m.name] and params_key(sg) == params_key(sig)
                               for k, mm, sg in self.members.get(target, []))
                    if not held and twin not in lines:
                        lines.append(twin)
        if ctor_holes and has_default_ctor and "instance void()" not in ctor_holes and not is_struct:
            lines.append(f"public {simple_name(target)}() {{ }}  // keeps the implicit default constructor")
        for (pname, static), acc in sorted(props.items()):
            m = acc.get("get") or acc.get("set")
            existing = [n for n in ((stub.methods if stub else {}))
                        if n in (f"get_{pname}", f"set_{pname}")]
            if existing or (stub and pname in stub.fields):
                self.manual.append((target, pname, "property", "property/field already in the stub with another shape"))
                lines.append(f"// MANUAL: property {pname} ({', '.join(sorted(acc))}): the stub already has "
                             f"{', '.join(existing) or 'a field'} -- extend/retype that one")
                continue
            if "get" in acc:
                g = acc["get"]
                ptype = g.ret
                index_params = g.params
            else:
                s = acc["set"]
                ptype = s.params[-1]
                index_params = s.params[:-1]
            if ptype.startswith("ref ") or "*" in ptype:
                self.manual.append((target, pname, "property", "ref/pointer property"))
                lines.append(f"// MANUAL: property {pname}: ref/pointer type")
                continue
            mods = [] if in_interface else [self.access(m.flags)]
            if static:
                mods.append("static")
            elif m.flags & 0x40 and not in_interface and not is_struct:
                mods.append("virtual")
            if index_params:
                ref = acc.get("get") or acc.get("set")
                plist = ", ".join(f"{t} {esc(ref.pnames.get(i + 1, f'i{i}'))}" for i, t in enumerate(index_params))
                body = []
                if "get" in acc:
                    body.append(f"get => {self.default_expr(ptype)};")
                if "set" in acc:
                    body.append("set { }")
                lines.append(f"{' '.join(mods)} {ptype} this[{plist}] {{ {' '.join(body)} }}")
            else:
                accessors = "get; set; " if "get" in acc else "set { } "   # set-only cannot be auto
                init = ""
                if "get" in acc and not in_interface:
                    init_expr = "new()" if (static and pname == "Singleton") else self.default_expr(ptype)
                    init = "" if init_expr == "default" else f" = {init_expr};"
                lines.append(f"{' '.join(mods)} {ptype} {esc(pname)} {{ {accessors}}}{init}")
            self.emitted_members += 1
        for (ename, static), acc in sorted(events.items()):
            a = acc.get("add") or acc.get("remove")
            dtype = a.params[0]
            mods = [] if in_interface else [self.access(a.flags)]
            if static:
                mods.append("static")
            lines.append(f"{' '.join(mods)} event {dtype} {esc(ename)};")
            self.emitted_members += 1
        return lines

    @staticmethod
    def _generic(gparams):
        return f"<{', '.join(gparams)}>" if gparams else ""

    def type_header(self, name, exists):
        """C# declaration head for `name` (a partial part of a stub type, or the new type)."""
        simple = simple_name(name)
        if exists:
            st = self.stubs[name]
            kind = "interface" if st.is_interface else "struct" if st.is_value else "class"
            return f"public partial {kind} {simple}{self._generic(st.gparams)}"
        ti = self.gen[name]
        generic = self._generic(ti.gparams)
        if ti.is_interface:
            return f"public partial interface {simple}{generic}"
        kind = "struct" if ti.is_value else "class"
        mods = "public static partial" if ti.is_static else "public partial"
        new = ""
        if "/" in name:  # a nested type that hides an inherited nested type of the same name
            for anc in self.chain(name.rsplit("/", 1)[0])[1:]:
                if f"{anc}/{simple}" in self.stubs or f"{anc}/{simple}" in self.gen:
                    new = "new "
                    break
        base_txt = ""
        if kind == "class" and ti.base and ti.base not in NO_BASE and not ti.is_static:
            base_txt = f" : {cs_name(generic_def(ti.base))}"
        return f"{mods.replace('public ', 'public ' + new, 1)} {kind} {simple}{generic}{base_txt}"

    def render_type(self, name, indent, out):
        pad = "    " * indent
        exists = name in self.stubs
        simple = simple_name(name)
        if not exists:
            ti = self.gen[name]
            if ti.is_enum:
                under = self.oracle.enum_underlying(name)
                flags_attr = "[global::System.Flags] " if ti.idx in self.oracle.flags_enums else ""
                vals = ", ".join(f"{esc(n)} = {v}" for n, v in self.oracle.enum_members(name))
                out.append(f"{pad}{flags_attr}public enum {simple} : {under} {{ {vals} }}")
                return
            if ti.is_delegate:
                ret, ps = self.oracle.invoke_signature(name)
                out.append(f"{pad}public delegate {ret} {simple}{self._generic(ti.gparams)}"
                           f"({', '.join(f'{t} {esc(n)}' for n, t in ps)});")
                return
        info = self.stubs[name] if exists else self.gen[name]
        body = self.type_body(name, exists, info.is_interface, info.is_value)
        out.append(f"{pad}{self.type_header(name, exists)}")
        out.append(f"{pad}{{")
        for line in body:
            out.append(f"{pad}    {line}")
        depth = name.count("/") + 1
        for child in sorted(n for n in self._closure if n.startswith(name + "/") and n.count("/") == depth):
            self.render_type(child, indent + 1, out)
        out.append(f"{pad}}}")

    def render(self):
        wanted = set(self.gen) | set(self.members)
        self._closure = set()
        for n in wanted:
            parts = n.split("/")
            for i in range(1, len(parts) + 1):
                self._closure.add("/".join(parts[:i]))
        by_ns = defaultdict(list)
        for n in sorted(x for x in self._closure if "/" not in x):
            by_ns[n.rsplit(".", 1)[0] if "." in n else ""].append(n)
        out = [
            "// <auto-generated>",
            "// Generated by tools/audit_godot_stub_refs.py --emit-stubs from the holes the audit found in",
            "// src/GodotStubs against lib/sts2.dll, using the real GodotSharp.dll as the signature oracle.",
            "// No-op headless stubs: methods do nothing and return what Godot returns for 'nothing' (empty string,",
            "// empty arrays/collections, Error.Failed, else default); properties are auto-properties; singletons exist.",
            "// Hand-written stubs live in the other files and always win -- this file only fills holes, and every",
            "// stub type is `partial` so it can add members without editing the hand-written declaration.",
            "// Do not hand-edit; re-run the audit after a game update and append the new output instead.",
            "// </auto-generated>",
            "#nullable disable",
            "#pragma warning disable CS0067, CS0108, CS0109, CS0114, CS0169, CS0414, CS0649, CS0660, CS0661, CS8618, CS0824",
            "",
        ]
        for ns in sorted(by_ns):
            out.append(f"namespace {ns}")
            out.append("{")
            for n in by_ns[ns]:
                self.render_type(n, 1, out)
            out.append("}")
            out.append("")
        return "\n".join(out)
