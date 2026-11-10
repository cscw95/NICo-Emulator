"""CEL subset (Common Expression Language) for Health Validation rules (F28).

Upstream: dsx-ai-factory/health-validation @ cc26732a510f evaluates catalog
check rules written in CEL (google/cel-spec, Apache-2.0). This is a small,
side-effect-free re-implementation of the subset the catalog rules use; it
never calls Python ``eval``.

Supported (cel-spec langdef.md):
  literals    int, double, string ('..' or ".."), true, false, null, [list], {map}
  operators   ! - (unary)  * / %  + -  < <= > >=  == !=  in  &&  ||  ?:
  access      a.b  a[i]  a["k"]
  functions   size(x) x.size() has(a.b) int() double() string() max() min()
              x.startsWith(s) x.endsWith(s) x.contains(s) x.matches(re)
  macros      x.all(v, p)  x.exists(v, p)  x.exists_one(v, p)  x.filter(v, p)  x.map(v, e)

Errors (unknown identifier, missing key, type mismatch) raise ``CelError``;
callers map them to severity UNKNOWN, as health-validation does for a
measurement that could not be collected. ``&&``/``||`` short-circuit and
absorb errors the way CEL's commutative logic does (``false && err`` → false).
"""
import re
from typing import Any, Dict, List, Optional, Tuple


class CelError(Exception):
    pass


_TOKEN = re.compile(r"""
    (?P<ws>\s+)
  | (?P<num>\d+\.\d*(?:[eE][+-]?\d+)?|\d+[eE][+-]?\d+|0x[0-9a-fA-F]+|\d+)u?
  | (?P<str>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
  | (?P<op>&&|\|\||==|!=|<=|>=|[-+*/%!<>?:.,()\[\]{}])
  | (?P<id>[A-Za-z_][A-Za-z0-9_]*)
""", re.X)
_RESERVED = {"true", "false", "null", "in"}
_MACROS = {"all", "exists", "exists_one", "filter", "map"}
MAX_LEN = 4096
MAX_DEPTH = 64


def tokenize(src: str) -> List[Tuple[str, str]]:
    if len(src) > MAX_LEN:
        raise CelError(f"expression longer than {MAX_LEN} characters")
    out, pos = [], 0
    while pos < len(src):
        m = _TOKEN.match(src, pos)
        if not m:
            raise CelError(f"unexpected character {src[pos]!r} at {pos}")
        pos = m.end()
        kind = m.lastgroup
        if kind == "ws":
            continue
        text = m.group(kind)
        if kind == "id" and text in _RESERVED:
            kind = "kw"
        out.append((kind, m.group(0) if kind == "num" else text))
    out.append(("eof", ""))
    return out


class _Parser:
    """Recursive descent over the cel-spec grammar:
    Expr = ConditionalOr ["?" ConditionalOr ":" Expr]."""

    def __init__(self, src: str):
        self.toks = tokenize(src)
        self.i = 0
        self.depth = 0

    def peek(self, k: int = 0) -> Tuple[str, str]:
        return self.toks[min(self.i + k, len(self.toks) - 1)]

    def take(self, text: Optional[str] = None) -> Tuple[str, str]:
        t = self.peek()
        if text is not None and t[1] != text:
            raise CelError(f"expected {text!r}, got {t[1] or 'end of input'!r}")
        self.i += 1
        return t

    def at(self, *texts: str) -> bool:
        t = self.peek()
        return t[0] in ("op", "kw") and t[1] in texts

    def parse(self):
        node = self.expr()
        if self.peek()[0] != "eof":
            raise CelError(f"unexpected {self.peek()[1]!r}")
        return node

    def expr(self):
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise CelError("expression nested too deeply")
        cond = self.binary(0)
        if self.at("?"):
            self.take("?")
            a = self.binary(0)
            self.take(":")
            b = self.expr()
            cond = ("cond", cond, a, b)
        self.depth -= 1
        return cond

    _LEVELS = [("||",), ("&&",), ("<", "<=", ">", ">=", "==", "!=", "in"), ("+", "-"), ("*", "/", "%")]

    def binary(self, level: int):
        if level == len(self._LEVELS):
            return self.unary()
        left = self.binary(level + 1)
        while self.at(*self._LEVELS[level]):
            op = self.take()[1]
            right = self.binary(level + 1)
            left = ("bin", op, left, right)
        return left

    def unary(self):
        if self.at("!"):
            self.take()
            return ("not", self.unary())
        if self.at("-"):
            self.take()
            return ("neg", self.unary())
        return self.member()

    def member(self):
        node = self.primary()
        while True:
            if self.at("."):
                self.take()
                name = self.take()
                if name[0] != "id":
                    raise CelError(f"expected field name, got {name[1]!r}")
                if self.at("("):
                    args = self.args()
                    if name[1] in _MACROS:
                        node = self.macro(name[1], node, args)
                    else:
                        node = ("call", name[1], node, args)
                else:
                    node = ("sel", node, name[1])
            elif self.at("["):
                self.take()
                idx = self.expr()
                self.take("]")
                node = ("idx", node, idx)
            else:
                return node

    @staticmethod
    def macro(name, target, args):
        if len(args) != 2 or args[0][0] != "ident":
            raise CelError(f"{name}() takes (variable, expression)")
        return ("macro", name, target, args[0][1], args[1])

    def args(self) -> list:
        self.take("(")
        out = []
        if not self.at(")"):
            out.append(self.expr())
            while self.at(","):
                self.take()
                out.append(self.expr())
        self.take(")")
        return out

    def primary(self):
        kind, text = self.peek()
        if kind == "num":
            self.take()
            raw = text.rstrip("u")
            if raw.startswith("0x"):
                return ("lit", int(raw, 16))
            return ("lit", float(raw) if any(c in raw for c in ".eE") else int(raw))
        if kind == "str":
            self.take()
            esc = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "'": "'", '"': '"'}
            return ("lit", re.sub(r"\\(.)", lambda m: esc.get(m.group(1), m.group(0)), text[1:-1]))
        if kind == "kw" and text in ("true", "false", "null"):
            self.take()
            return ("lit", {"true": True, "false": False, "null": None}[text])
        if kind == "id":
            self.take()
            if self.at("("):
                return ("call", text, None, self.args())
            return ("ident", text)
        if self.at("("):
            self.take()
            e = self.expr()
            self.take(")")
            return e
        if self.at("["):
            self.take()
            items = []
            if not self.at("]"):
                items.append(self.expr())
                while self.at(","):
                    self.take()
                    items.append(self.expr())
            self.take("]")
            return ("list", items)
        if self.at("{"):
            self.take()
            pairs = []
            if not self.at("}"):
                while True:
                    k = self.expr()
                    self.take(":")
                    pairs.append((k, self.expr()))
                    if not self.at(","):
                        break
                    self.take()
            self.take("}")
            return ("map", pairs)
        raise CelError(f"unexpected {text or 'end of input'!r}")


def compile_expr(src: str):
    """Parse once → AST (tuples). Raises CelError on a syntax error."""
    if not isinstance(src, str) or not src.strip():
        raise CelError("expression is empty")
    return _Parser(src).parse()


def identifiers(ast) -> set:
    """Top-level identifiers the expression reads (its sensors/signals)."""
    out, bound = set(), set()

    def walk(n, scope):
        if not isinstance(n, tuple):
            return
        tag = n[0]
        if tag == "ident":
            if n[1] not in scope:
                out.add(n[1])
        elif tag == "macro":
            walk(n[2], scope)
            walk(n[4], scope | {n[3]})
        elif tag == "call":
            if n[2] is not None:
                walk(n[2], scope)
            for a in n[3]:
                walk(a, scope)
        elif tag in ("list",):
            for a in n[1]:
                walk(a, scope)
        elif tag == "map":
            for k, v in n[1]:
                walk(k, scope)
                walk(v, scope)
        else:
            for a in n[1:]:
                walk(a, scope)
    walk(ast, bound)
    return out


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _arith(op, a, b):
    if op == "+" and isinstance(a, str) and isinstance(b, str):
        return a + b
    if op == "+" and isinstance(a, list) and isinstance(b, list):
        return a + b
    if not (_num(a) and _num(b)):
        raise CelError(f"no such overload: {type(a).__name__} {op} {type(b).__name__}")
    if op == "+":
        return a + b
    if op == "-":
        return a - b
    if op == "*":
        return a * b
    if b == 0:
        raise CelError("division by zero")
    if op == "/":
        return a // b if isinstance(a, int) and isinstance(b, int) else a / b
    return a % b


def _compare(op, a, b):
    if op in ("==", "!="):
        eq = a == b and (type(a) is type(b) or (_num(a) and _num(b)))
        return eq if op == "==" else not eq
    if not ((_num(a) and _num(b)) or (isinstance(a, str) and isinstance(b, str))):
        raise CelError(f"no such overload: {type(a).__name__} {op} {type(b).__name__}")
    return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]


def _bool(v):
    if not isinstance(v, bool):
        raise CelError(f"expected bool, got {type(v).__name__}")
    return v


def evaluate(ast, env: Dict[str, Any]) -> Any:
    tag = ast[0]
    if tag == "lit":
        return ast[1]
    if tag == "ident":
        if ast[1] not in env:
            raise CelError(f"undeclared reference to '{ast[1]}'")
        return env[ast[1]]
    if tag == "sel":
        obj = evaluate(ast[1], env)
        if not isinstance(obj, dict) or ast[2] not in obj:
            raise CelError(f"no such key: {ast[2]}")
        return obj[ast[2]]
    if tag == "idx":
        obj, key = evaluate(ast[1], env), evaluate(ast[2], env)
        try:
            if isinstance(obj, list) and not isinstance(key, int):
                raise TypeError
            return obj[key]
        except (KeyError, IndexError, TypeError):
            raise CelError(f"no such key or index: {key!r}") from None
    if tag == "list":
        return [evaluate(x, env) for x in ast[1]]
    if tag == "map":
        return {evaluate(k, env): evaluate(v, env) for k, v in ast[1]}
    if tag == "not":
        return not _bool(evaluate(ast[1], env))
    if tag == "neg":
        v = evaluate(ast[1], env)
        if not _num(v):
            raise CelError("no such overload: -" + type(v).__name__)
        return -v
    if tag == "cond":
        return evaluate(ast[2] if _bool(evaluate(ast[1], env)) else ast[3], env)
    if tag == "bin":
        op = ast[1]
        if op in ("&&", "||"):
            # CEL commutative logic: a decisive side wins over an error on the other
            short = op == "||"
            err = None
            for side in (ast[2], ast[3]):
                try:
                    if _bool(evaluate(side, env)) is short:
                        return short
                except CelError as e:
                    err = e
            if err:
                raise err
            return not short
        a, b = evaluate(ast[2], env), evaluate(ast[3], env)
        if op == "in":
            if isinstance(b, (list, dict)):
                return a in b
            raise CelError("'in' needs a list or map")
        if op in ("<", "<=", ">", ">=", "==", "!="):
            return _compare(op, a, b)
        return _arith(op, a, b)
    if tag == "macro":
        _, name, target, var, body = ast
        coll = evaluate(target, env)
        if isinstance(coll, dict):
            coll = list(coll.keys())
        if not isinstance(coll, list):
            raise CelError(f"{name}() needs a list or map")
        if name == "map":
            return [evaluate(body, {**env, var: x}) for x in coll]
        hits = [x for x in coll if _bool(evaluate(body, {**env, var: x}))]
        return {"all": len(hits) == len(coll), "exists": bool(hits),
                "exists_one": len(hits) == 1, "filter": hits}[name]
    if tag == "call":
        return _call(ast[1], ast[2], ast[3], env)
    raise CelError(f"bad node {tag}")


def _call(name, target, args, env):
    if name == "has":
        if len(args) != 1 or args[0][0] != "sel":
            raise CelError("has() needs a field selection, e.g. has(a.b)")
        try:
            obj = evaluate(args[0][1], env)
        except CelError:
            return False
        return isinstance(obj, dict) and args[0][2] in obj
    vals = [evaluate(a, env) for a in args]
    if target is not None:
        vals.insert(0, evaluate(target, env))
    if name == "size":
        if len(vals) != 1 or not isinstance(vals[0], (str, list, dict)):
            raise CelError("size() needs a string, list or map")
        return len(vals[0])
    if name in ("max", "min"):
        xs = vals[0] if len(vals) == 1 and isinstance(vals[0], list) else vals
        if not xs or not all(_num(x) for x in xs):
            raise CelError(f"{name}() needs numbers")
        return max(xs) if name == "max" else min(xs)
    if name in ("int", "double", "string"):
        try:
            return {"int": int, "double": float, "string": str}[name](vals[0])
        except (ValueError, TypeError, IndexError):
            raise CelError(f"{name}() conversion failed") from None
    if name in ("startsWith", "endsWith", "contains", "matches"):
        if len(vals) != 2 or not all(isinstance(v, str) for v in vals):
            raise CelError(f"{name}() needs strings")
        s, p = vals
        if name == "matches":
            try:
                return re.search(p, s) is not None
            except re.error as e:
                raise CelError(f"bad regex: {e}") from None
        return {"startsWith": s.startswith, "endsWith": s.endswith,
                "contains": s.__contains__}[name](p)
    raise CelError(f"undeclared function '{name}'")


def run(src: str, env: Dict[str, Any]) -> Any:
    return evaluate(compile_expr(src), env)
