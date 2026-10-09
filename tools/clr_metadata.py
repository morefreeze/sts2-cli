"""A small, dependency-free ECMA-335 (.NET assembly) metadata reader.

Just enough for tools/audit_godot_stub_refs.py: PE/CLI headers, the #~ tables, the #Strings /
#Blob heaps and signature decoding.  There is no ildasm/ilspy/dnfile on the build machine, and the
audit has to run anywhere ``pytest tests/`` runs, so this lives in the repo.

Types are named ``Namespace.Name`` with ``/`` for nesting and the CLR's backtick arity
(``Godot.Collections.Array`1``).  Signatures decode to canonical strings with no assembly
qualification, so a member reference in one assembly compares equal to the matching definition in
another (``instance void(Godot.StringName,Godot.Callable,uint32)``).  A second ``cs`` mode renders
C# syntax for the stub emitter.
"""
from __future__ import annotations

import struct
from collections import defaultdict


class MetadataError(Exception):
    pass


CODED = {
    "TypeDefOrRef": (2, [0x02, 0x01, 0x1B]),
    "HasConstant": (2, [0x04, 0x08, 0x17]),
    "HasCustomAttribute": (5, [0x06, 0x04, 0x01, 0x02, 0x08, 0x09, 0x0A, 0x00, 0x0E, 0x17, 0x14,
                               0x11, 0x1A, 0x1B, 0x20, 0x23, 0x26, 0x27, 0x28, 0x2A, 0x2C, 0x2B]),
    "HasFieldMarshal": (1, [0x04, 0x08]),
    "HasDeclSecurity": (2, [0x02, 0x06, 0x20]),
    "MemberRefParent": (3, [0x02, 0x01, 0x1A, 0x06, 0x1B]),
    "HasSemantics": (1, [0x14, 0x17]),
    "MethodDefOrRef": (1, [0x06, 0x0A]),
    "MemberForwarded": (1, [0x04, 0x06]),
    "Implementation": (2, [0x26, 0x23, 0x27]),
    "CustomAttributeType": (3, [None, None, 0x06, 0x0A, None]),
    "ResolutionScope": (2, [0x00, 0x1A, 0x23, 0x01]),
    "TypeOrMethodDef": (1, [0x02, 0x06]),
}


def _t(n):
    return ("i", n)


def _c(n):
    return ("c", n)


# column kinds: u1/u2/u4 fixed, S/G/B heap indexes, ("i", table) simple index, ("c", name) coded
SCHEMA = {
    0x00: ("Module", ["u2", "S", "G", "G", "G"]),
    0x01: ("TypeRef", [_c("ResolutionScope"), "S", "S"]),
    0x02: ("TypeDef", ["u4", "S", "S", _c("TypeDefOrRef"), _t(0x04), _t(0x06)]),
    0x03: ("FieldPtr", [_t(0x04)]),
    0x04: ("Field", ["u2", "S", "B"]),
    0x05: ("MethodPtr", [_t(0x06)]),
    0x06: ("MethodDef", ["u4", "u2", "u2", "S", "B", _t(0x08)]),
    0x07: ("ParamPtr", [_t(0x08)]),
    0x08: ("Param", ["u2", "u2", "S"]),
    0x09: ("InterfaceImpl", [_t(0x02), _c("TypeDefOrRef")]),
    0x0A: ("MemberRef", [_c("MemberRefParent"), "S", "B"]),
    0x0B: ("Constant", ["u1", "u1", _c("HasConstant"), "B"]),
    0x0C: ("CustomAttribute", [_c("HasCustomAttribute"), _c("CustomAttributeType"), "B"]),
    0x0D: ("FieldMarshal", [_c("HasFieldMarshal"), "B"]),
    0x0E: ("DeclSecurity", ["u2", _c("HasDeclSecurity"), "B"]),
    0x0F: ("ClassLayout", ["u2", "u4", _t(0x02)]),
    0x10: ("FieldLayout", ["u4", _t(0x04)]),
    0x11: ("StandAloneSig", ["B"]),
    0x12: ("EventMap", [_t(0x02), _t(0x14)]),
    0x13: ("EventPtr", [_t(0x14)]),
    0x14: ("Event", ["u2", "S", _c("TypeDefOrRef")]),
    0x15: ("PropertyMap", [_t(0x02), _t(0x17)]),
    0x16: ("PropertyPtr", [_t(0x17)]),
    0x17: ("Property", ["u2", "S", "B"]),
    0x18: ("MethodSemantics", ["u2", _t(0x06), _c("HasSemantics")]),
    0x19: ("MethodImpl", [_t(0x02), _c("MethodDefOrRef"), _c("MethodDefOrRef")]),
    0x1A: ("ModuleRef", ["S"]),
    0x1B: ("TypeSpec", ["B"]),
    0x1C: ("ImplMap", ["u2", _c("MemberForwarded"), "S", _t(0x1A)]),
    0x1D: ("FieldRVA", ["u4", _t(0x04)]),
    0x1E: ("EncLog", ["u4", "u4"]),
    0x1F: ("EncMap", ["u4"]),
    0x20: ("Assembly", ["u4", "u2", "u2", "u2", "u2", "u4", "B", "S", "S"]),
    0x21: ("AssemblyProcessor", ["u4"]),
    0x22: ("AssemblyOS", ["u4", "u4", "u4"]),
    0x23: ("AssemblyRef", ["u2", "u2", "u2", "u2", "u4", "B", "S", "S", "B"]),
    0x24: ("AssemblyRefProcessor", ["u4", _t(0x23)]),
    0x25: ("AssemblyRefOS", ["u4", "u4", "u4", _t(0x23)]),
    0x26: ("File", ["u4", "S", "B"]),
    0x27: ("ExportedType", ["u4", "u4", "S", "S", _c("Implementation")]),
    0x28: ("ManifestResource", ["u4", "u4", "S", _c("Implementation")]),
    0x29: ("NestedClass", [_t(0x02), _t(0x02)]),
    0x2A: ("GenericParam", ["u2", "u2", _c("TypeOrMethodDef"), "S"]),
    0x2B: ("MethodSpec", [_c("MethodDefOrRef"), "B"]),
    0x2C: ("GenericParamConstraint", [_t(0x2A), _c("TypeDefOrRef")]),
}

PRIMS = {0x01: "void", 0x02: "bool", 0x03: "char", 0x04: "int8", 0x05: "uint8", 0x06: "int16",
         0x07: "uint16", 0x08: "int32", 0x09: "uint32", 0x0A: "int64", 0x0B: "uint64",
         0x0C: "float32", 0x0D: "float64", 0x0E: "string", 0x16: "typedref", 0x18: "native int",
         0x19: "native uint", 0x1C: "object"}
CS_PRIMS = {0x01: "void", 0x02: "bool", 0x03: "char", 0x04: "sbyte", 0x05: "byte", 0x06: "short",
            0x07: "ushort", 0x08: "int", 0x09: "uint", 0x0A: "long", 0x0B: "ulong", 0x0C: "float",
            0x0D: "double", 0x0E: "string", 0x16: "System.TypedReference", 0x18: "nint",
            0x19: "nuint", 0x1C: "object"}


class Metadata:
    """Parsed metadata of one managed assembly (read-only)."""

    def __init__(self, path):
        self.path = path
        with open(path, "rb") as f:
            self.data = f.read()
        self._parse_pe()
        self._parse_streams()
        self._parse_tables()
        self._nested = None
        self._type_name_cache = {}
        self._typeref_name_cache = {}

    # ---- PE / CLI header -------------------------------------------------------------------
    def _parse_pe(self):
        d = self.data
        if d[:2] != b"MZ":
            raise MetadataError(f"{self.path}: not a PE file")
        pe = struct.unpack_from("<I", d, 0x3C)[0]
        if d[pe:pe + 4] != b"PE\0\0":
            raise MetadataError(f"{self.path}: bad PE signature")
        nsec, = struct.unpack_from("<H", d, pe + 6)
        optsize, = struct.unpack_from("<H", d, pe + 20)
        opt = pe + 24
        magic, = struct.unpack_from("<H", d, opt)
        dd = opt + (96 if magic == 0x10B else 112)
        cli_rva, _cli_size = struct.unpack_from("<II", d, dd + 14 * 8)
        if cli_rva == 0:
            raise MetadataError(f"{self.path}: not a managed assembly")
        sec = opt + optsize
        self.sections = []
        for i in range(nsec):
            vsize, vaddr, rawsize, rawptr = struct.unpack_from("<IIII", d, sec + i * 40 + 8)
            self.sections.append((vaddr, max(vsize, rawsize), rawptr))
        cli = self._rva(cli_rva)
        md_rva, _md_size = struct.unpack_from("<II", d, cli + 8)
        self.md_off = self._rva(md_rva)

    def _rva(self, rva):
        for vaddr, size, rawptr in self.sections:
            if vaddr <= rva < vaddr + size:
                return rva - vaddr + rawptr
        raise MetadataError(f"{self.path}: RVA {rva:#x} not in any section")

    # ---- streams ---------------------------------------------------------------------------
    def _parse_streams(self):
        d, o = self.data, self.md_off
        if struct.unpack_from("<I", d, o)[0] != 0x424A5342:
            raise MetadataError(f"{self.path}: bad metadata signature")
        vlen, = struct.unpack_from("<I", d, o + 12)
        p = o + 16 + vlen + 2
        nstreams, = struct.unpack_from("<H", d, p)
        p += 2
        self.streams = {}
        for _ in range(nstreams):
            off, size = struct.unpack_from("<II", d, p)
            p += 8
            end = d.index(b"\0", p)
            name = d[p:end].decode("ascii")
            p = (end + 1 + 3) & ~3
            self.streams[name] = (o + off, size)
        self.strings_off = self.streams["#Strings"][0]
        self.blob_off = self.streams["#Blob"][0]
        self._str_cache = {}

    def string(self, idx):
        s = self._str_cache.get(idx)
        if s is None:
            start = self.strings_off + idx
            s = self.data[start:self.data.index(b"\0", start)].decode("utf-8", "replace")
            self._str_cache[idx] = s
        return s

    def blob(self, idx):
        p = self.blob_off + idx
        b0 = self.data[p]
        if b0 & 0x80 == 0:
            n, p = b0, p + 1
        elif b0 & 0xC0 == 0x80:
            n, p = ((b0 & 0x3F) << 8) | self.data[p + 1], p + 2
        else:
            n = ((b0 & 0x1F) << 24) | (self.data[p + 1] << 16) | (self.data[p + 2] << 8) | self.data[p + 3]
            p += 4
        return self.data[p:p + n]

    # ---- tables ----------------------------------------------------------------------------
    def _parse_tables(self):
        d = self.data
        p = self.streams["#~"][0] if "#~" in self.streams else self.streams["#-"][0]
        heap_sizes = d[p + 6]
        valid, = struct.unpack_from("<Q", d, p + 8)
        p += 24
        counts = {}
        for t in range(64):
            if valid >> t & 1:
                counts[t], = struct.unpack_from("<I", d, p)
                p += 4
        if heap_sizes & 0x40:
            p += 4
        self.counts = counts
        heap_w = {"S": 4 if heap_sizes & 1 else 2, "G": 4 if heap_sizes & 2 else 2,
                  "B": 4 if heap_sizes & 4 else 2}

        def width(col):
            if col in ("u1", "u2", "u4"):
                return {"u1": 1, "u2": 2, "u4": 4}[col]
            if isinstance(col, str):
                return heap_w[col]
            kind, arg = col
            if kind == "i":
                return 4 if counts.get(arg, 0) >= 1 << 16 else 2
            bits, tables = CODED[arg]
            biggest = max(counts.get(t, 0) for t in tables if t is not None)
            return 4 if biggest >= 1 << (16 - bits) else 2

        self.rows = {}
        for t in sorted(counts):
            if t not in SCHEMA:
                raise MetadataError(f"{self.path}: unsupported metadata table {t:#x}")
            widths = [width(c) for c in SCHEMA[t][1]]
            fmt = "<" + "".join({1: "B", 2: "H", 4: "I"}[w] for w in widths)
            size = sum(widths)
            n = counts[t]
            self.rows[t] = list(struct.iter_unpack(fmt, d[p:p + size * n]))
            p += size * n

    def table(self, t):
        return self.rows.get(t, [])

    def row(self, t, idx):  # idx is 1-based
        return self.rows[t][idx - 1]

    @staticmethod
    def decode_coded(kind, value):
        bits, tables = CODED[kind]
        return tables[value & ((1 << bits) - 1)], value >> bits

    # ---- names -----------------------------------------------------------------------------
    @property
    def nested(self):
        if self._nested is None:
            self._nested = {n: e for n, e in self.table(0x29)}
        return self._nested

    @staticmethod
    def qualify(ns, name):
        return f"{ns}.{name}" if ns else name

    def typedef_name(self, idx):
        name = self._type_name_cache.get(idx)
        if name is None:
            r = self.row(0x02, idx)
            name = self.qualify(self.string(r[2]), self.string(r[1]))
            enc = self.nested.get(idx)
            if enc:
                name = self.typedef_name(enc) + "/" + self.string(r[1])
            self._type_name_cache[idx] = name
        return name

    def typeref_name(self, idx):
        name = self._typeref_name_cache.get(idx)
        if name is None:
            scope, nm, ns = self.row(0x01, idx)
            tbl, sidx = self.decode_coded("ResolutionScope", scope)
            if tbl == 0x01:
                name = self.typeref_name(sidx) + "/" + self.string(nm)
            else:
                name = self.qualify(self.string(ns), self.string(nm))
            self._typeref_name_cache[idx] = name
        return name

    def typeref_assembly(self, idx):
        """Name of the assembly a TypeRef is scoped to (follows nesting), or None."""
        tbl, sidx = self.decode_coded("ResolutionScope", self.row(0x01, idx)[0])
        if tbl == 0x01:
            return self.typeref_assembly(sidx)
        if tbl == 0x23:
            return self.string(self.row(0x23, sidx)[6])
        if tbl == 0x1A:
            return "module:" + self.string(self.row(0x1A, sidx)[0])
        return None  # Module -> this assembly

    def assembly_name(self):
        asm = self.table(0x20)
        return self.string(asm[0][7]) if asm else None

    # ---- signatures ------------------------------------------------------------------------
    def type_from_coded(self, value, **kw):
        tbl, idx = self.decode_coded("TypeDefOrRef", value)
        if tbl == 0x02:
            return self.typedef_name(idx)
        if tbl == 0x01:
            return self.typeref_name(idx)
        return self.typespec_str(idx, **kw)

    def typespec_str(self, idx, **kw):
        return SigReader(self, self.blob(self.row(0x1B, idx)[0]), **kw).read_type()

    def typespec_def(self, idx):
        """Generic definition name of a TypeSpec (GENERICINST only), else None."""
        sr = SigReader(self, self.blob(self.row(0x1B, idx)[0]))
        if sr.peek() == 0x15:
            sr.read_type()
            return sr.last_generic_def
        return None


def cs_name(name):
    """Canonical type name -> C# (global::Godot.Control.SizeFlags; arity backticks dropped)."""
    out = []
    for part in name.split("/"):
        out.append(part.split("`", 1)[0])
    return "global::" + ".".join(out)


class SigReader:
    def __init__(self, md, blob, usage=None, cs=False, tparams=(), mparams=()):
        self.md, self.b, self.p = md, blob, 0
        self.usage = usage  # optional list collecting (marker, typename) for CLASS/VALUETYPE uses
        self.cs, self.tparams, self.mparams = cs, tparams, mparams
        self.last_generic_def = None

    def peek(self):
        return self.b[self.p]

    def byte(self):
        v = self.b[self.p]
        self.p += 1
        return v

    def uint(self):
        b0 = self.byte()
        if b0 & 0x80 == 0:
            return b0
        if b0 & 0xC0 == 0x80:
            return ((b0 & 0x3F) << 8) | self.byte()
        return ((b0 & 0x1F) << 24) | (self.byte() << 16) | (self.byte() << 8) | self.byte()

    def coded_type(self):
        return self.md.type_from_coded(self.uint())

    def skip_mods(self):
        while self.peek() in (0x1F, 0x20, 0x45):  # modreq / modopt / pinned
            if self.byte() != 0x45:
                self.uint()

    def _name(self, n):
        return cs_name(n) if self.cs else n

    def read_type(self):
        self.skip_mods()
        t = self.byte()
        prims = CS_PRIMS if self.cs else PRIMS
        if t in prims:
            return prims[t]
        if t in (0x11, 0x12):
            name = self.coded_type()
            if self.usage is not None:
                self.usage.append(("valuetype" if t == 0x11 else "class", name))
            return self._name(name)
        if t == 0x0F:
            return self.read_type() + "*"
        if t == 0x10:
            return "ref " + self.read_type()
        if t == 0x1D:
            return self.read_type() + "[]"
        if t == 0x14:
            elem = self.read_type()
            rank = self.uint()
            for _ in range(self.uint()):
                self.uint()
            for _ in range(self.uint()):
                self.uint()
            return f"{elem}[{',' * (rank - 1)}]"
        if t == 0x13:
            n = self.uint()
            return (self.tparams[n] if self.cs and n < len(self.tparams) else f"T{n}") if self.cs else f"!{n}"
        if t == 0x1E:
            n = self.uint()
            return (self.mparams[n] if self.cs and n < len(self.mparams) else f"M{n}") if self.cs else f"!!{n}"
        if t == 0x15:
            marker = self.byte()
            name = self.coded_type()
            if self.usage is not None:
                self.usage.append(("valuetype" if marker == 0x11 else "class", name))
            args = [self.read_type() for _ in range(self.uint())]
            self.last_generic_def = name
            return f"{self._name(name)}<{','.join(args)}>"
        if t == 0x1B:
            return "fnptr(" + self.method_sig() + ")"
        if t in (0x21, 0x40):  # internal / modifier: ignore
            return "?"
        raise MetadataError(f"unsupported element type {t:#x} in signature")

    def method_parts(self):
        """-> (calling convention byte, generic param count, return type, [param types])."""
        cc = self.byte()
        ngen = self.uint() if cc & 0x10 else 0
        n = self.uint()
        ret = self.read_type()
        params = []
        for _ in range(n):
            if self.p < len(self.b) and self.peek() == 0x41:  # vararg sentinel
                self.byte()
                params.append("...")
            params.append(self.read_type())
        return cc, ngen, ret, params

    def method_sig(self):
        cc, ngen, ret, params = self.method_parts()
        s = f"{'instance ' if cc & 0x20 else ''}{ret}({','.join(params)})"
        return s + (f"<{ngen}>" if ngen else "")

    def signature(self):
        """Decode a MethodDef/MemberRef/Field/Property/LocalSig blob -> (kind, canonical string)."""
        cc = self.peek() & 0x0F
        if cc == 0x06:
            self.byte()
            return "field", self.read_type()
        if cc == 0x07:
            self.byte()
            return "locals", ",".join(self.read_type() for _ in range(self.uint()))
        if cc == 0x08:
            return "property", self.method_sig()
        if cc == 0x0A:  # method instantiation
            self.byte()
            return "methodspec", ",".join(self.read_type() for _ in range(self.uint()))
        return "method", self.method_sig()


def generic_def(name):
    """'A.B<x,y>' -> 'A.B' (also for names without arguments)."""
    return name.split("<", 1)[0]


def split_sig(sig):
    """Canonical method signature -> (prefix 'instance '|'', return type, params string, '<n>' suffix)."""
    prefix = "instance " if sig.startswith("instance ") else ""
    body = sig[len(prefix):]
    depth = 0
    for i, ch in enumerate(body):
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        elif ch == "(" and depth == 0 and not body[:i].endswith("fnptr"):
            break
    else:
        raise MetadataError(f"cannot split signature {sig!r}")
    ret = body[:i]
    rest = body[i + 1:]
    close = rest.rindex(")")
    return prefix, ret, rest[:close], rest[close + 1:]


def params_key(sig):
    """A method signature without its return type (what a C# overload set is keyed on)."""
    prefix, _ret, params, tail = split_sig(sig)
    return f"{prefix}({params}){tail}"


class TypeInfo:
    """One TypeDef of an indexed assembly."""
    __slots__ = ("name", "idx", "flags", "base", "is_value", "is_enum", "is_interface", "is_sealed",
                 "is_static", "is_delegate", "methods", "fields", "interfaces", "gparams")

    def __init__(self, name, idx, flags, base):
        self.name, self.idx, self.flags, self.base = name, idx, flags, base
        self.is_interface = bool(flags & 0x20)
        self.is_sealed = bool(flags & 0x100)
        self.is_static = bool(flags & 0x80) and self.is_sealed and not self.is_interface
        self.is_value = base in ("System.ValueType", "System.Enum")
        self.is_enum = base == "System.Enum"
        self.is_delegate = base == "System.MulticastDelegate"
        self.methods = defaultdict(set)   # name -> {canonical signature}
        self.fields = defaultdict(set)    # name -> {canonical type}
        self.interfaces = []
        self.gparams = []


def index_types(md):
    """name -> TypeInfo for every TypeDef (members as canonical signature sets)."""
    types = {}
    typedefs = md.table(0x02)
    methods, fields = md.table(0x06), md.table(0x04)
    gp = defaultdict(list)
    for num, _flags, owner, name in md.table(0x2A):
        tbl, idx = md.decode_coded("TypeOrMethodDef", owner)
        if tbl == 0x02:
            gp[idx].append((num, md.string(name)))
    for i, r in enumerate(typedefs, start=1):
        flags, _name_i, _ns_i, ext, f_start, m_start = r
        base = md.type_from_coded(ext) if ext else None
        ti = TypeInfo(md.typedef_name(i), i, flags, base)
        ti.gparams = [n for _, n in sorted(gp.get(i, []))]
        f_end = typedefs[i][4] if i < len(typedefs) else len(fields) + 1
        m_end = typedefs[i][5] if i < len(typedefs) else len(methods) + 1
        for fi in range(f_start, f_end):
            fr = fields[fi - 1]
            ti.fields[md.string(fr[1])].add(SigReader(md, md.blob(fr[2])).signature()[1])
        for mi in range(m_start, m_end):
            mr = methods[mi - 1]
            ti.methods[md.string(mr[3])].add(SigReader(md, md.blob(mr[4])).signature()[1])
        types[ti.name] = ti
    for cls, iface in md.table(0x09):
        types[md.typedef_name(cls)].interfaces.append(md.type_from_coded(iface))
    return types


def split_generic(name):
    """'A.B`2<x,C<y,z>>' -> ('A.B`2', ['x', 'C<y,z>']); no arguments -> (name, [])."""
    if "<" not in name or not name.endswith(">"):
        return name, []
    head, rest = name.split("<", 1)
    rest, args, depth, cur = rest[:-1], [], 0, ""
    for ch in rest:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        if ch == "," and depth == 0:
            args.append(cur)
            cur = ""
        else:
            cur += ch
    args.append(cur)
    return head, args


def subst_vars(text, args):
    """Replace the type parameters !0, !1 ... in a canonical signature by `args` (method !!n untouched)."""
    if not args:
        return text
    import re
    return re.sub(r"(?<!!)!(\d+)", lambda m: args[int(m.group(1))] if int(m.group(1)) < len(args) else m.group(0), text)
