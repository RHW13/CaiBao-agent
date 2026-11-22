"""白名单公式求值器（基于 ast 模块，严禁 eval）。

支持：四则运算 + 括号 + sum/avg/yoy/nz/abs 函数 + 变量（科目键）。
AST JSON 格式：
  - 变量: {"var": "科目名"}
  - 常量: {"const": 1.23}
  - 二元: {"op": "+|-|*|/", "left": ..., "right": ...}
  - 函数: {"func": "sum|avg|yoy|nz|abs", "args": [...]}

nz(x)：科目缺失（None）时按 0 计，用于报表中空白的可选科目（如交易性金融资产）。
abs(x)：取绝对值，用于利息支出等以负数列示的科目。
validate_ast(x)：校验并规范化外部来源（LLM）的 AST JSON，白名单外的结构一律拒绝。
"""
import ast


class FormulaError(Exception):
    """公式语法或白名单校验失败。"""


_ALLOWED_OPS = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/"}
_ALLOWED_FUNCS = {"sum", "avg", "yoy", "nz", "abs"}
# 函数参数个数约束：sum/avg 至少 1 个，yoy/nz/abs 固定个数
_FUNC_ARITY = {"sum": None, "avg": None, "yoy": 2, "nz": 1, "abs": 1}
# 外部 AST（如 LLM 返回的 JSON）的嵌套深度与节点总数上限，防止深层递归与超大表达式
_AST_MAX_DEPTH = 32
_AST_MAX_NODES = 256


def _node_to_json(node):
    if isinstance(node, ast.Expression):
        return _node_to_json(node.body)
    if isinstance(node, ast.BinOp):
        op = _ALLOWED_OPS.get(type(node.op))
        if op is None:
            raise FormulaError("不支持的运算符（仅允许 + - * /）。")
        return {"op": op, "left": _node_to_json(node.left), "right": _node_to_json(node.right)}
    if isinstance(node, ast.Name):
        return {"var": node.id}
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return {"const": float(node.value)}
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        inner = _node_to_json(node.operand)
        if isinstance(node.op, ast.UAdd):
            return inner
        return {"op": "*", "left": {"const": -1.0}, "right": inner}
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _ALLOWED_FUNCS:
        return {"func": node.func.id, "args": [_node_to_json(a) for a in node.args]}
    raise FormulaError("表达式包含不支持的内容（仅允许四则运算、括号与 sum/avg/yoy/nz）。")


def parse_formula(text: str) -> dict:
    """把公式字符串解析为 JSON AST。"""
    if not text or not str(text).strip():
        raise FormulaError("公式为空。")
    try:
        tree = ast.parse(str(text), mode="eval")
    except SyntaxError:
        raise FormulaError("公式语法错误，请检查括号与运算符。")
    return _node_to_json(tree)


def validate_ast(node, _depth: int = 0, _budget: list = None) -> dict:
    """校验并规范化外部来源（如 LLM 返回）的 AST JSON。

    parse_formula 由白名单语法树构造，天然可信；而模型输出属于外部数据，
    可能包含非法键、非数值常量、未知函数或超深嵌套，直接交给 evaluate 会
    得到错误结果或触发递归异常。此处按同一白名单重建 AST，任何不符即抛
    FormulaError，调用方回退到规则通道或提示不可解析。
    """
    if _budget is None:
        _budget = [_AST_MAX_NODES]
    if _depth > _AST_MAX_DEPTH:
        raise FormulaError("表达式嵌套过深，请简化公式。")
    _budget[0] -= 1
    if _budget[0] < 0:
        raise FormulaError("表达式过长，请简化公式。")
    if not isinstance(node, dict):
        raise FormulaError("公式结构不合法。")

    if "var" in node:
        name = node["var"]
        if not isinstance(name, str) or not name.strip():
            raise FormulaError("变量名不合法。")
        return {"var": name.strip()}
    if "const" in node:
        value = node["const"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise FormulaError("常量必须是数字。")
        return {"const": float(value)}
    if "op" in node:
        op = node["op"]
        if not isinstance(op, str) or op not in _ALLOWED_OPS.values():
            raise FormulaError("不支持的运算符（仅允许 + - * /）。")
        return {
            "op": op,
            "left": validate_ast(node.get("left"), _depth + 1, _budget),
            "right": validate_ast(node.get("right"), _depth + 1, _budget),
        }
    if "func" in node:
        fn = node["func"]
        if not isinstance(fn, str) or fn not in _ALLOWED_FUNCS:
            raise FormulaError("不支持函数（仅允许 sum/avg/yoy/nz/abs）。")
        args = node.get("args")
        if not isinstance(args, list) or not args:
            raise FormulaError("函数参数不合法。")
        arity = _FUNC_ARITY.get(fn)
        if arity is not None and len(args) != arity:
            raise FormulaError(f"{fn} 需要 {arity} 个参数。")
        return {
            "func": fn,
            "args": [validate_ast(a, _depth + 1, _budget) for a in args],
        }
    raise FormulaError("公式结构不合法。")


def iter_vars(ast_node: dict, skip_nz: bool = False) -> list:
    """按出现顺序返回公式引用的变量名（可跳过 nz() 包裹的变量）。"""
    names = []

    def walk(node, in_nz=False):
        if not isinstance(node, dict):
            return
        if "var" in node:
            if not (skip_nz and in_nz):
                names.append(str(node["var"]))
        elif "op" in node:
            walk(node["left"], in_nz)
            walk(node["right"], in_nz)
        elif "func" in node:
            child_nz = in_nz or node["func"] == "nz"
            for arg in node["args"]:
                walk(arg, child_nz)

    walk(ast_node)
    return names


def evaluate(ast_node: dict, data: dict):
    """在 data（科目键 -> 数值）上递归求值；缺失或分母为 0 返回 None。"""
    if not isinstance(ast_node, dict):
        return None
    if "var" in ast_node:
        return data.get(ast_node["var"])
    if "const" in ast_node:
        return ast_node["const"]
    if "op" in ast_node:
        left = evaluate(ast_node["left"], data)
        right = evaluate(ast_node["right"], data)
        if left is None or right is None:
            return None
        op = ast_node["op"]
        if op == "+":
            return left + right
        if op == "-":
            return left - right
        if op == "*":
            return left * right
        if op == "/":
            return None if right == 0 else left / right
    if "func" in ast_node:
        fn = ast_node["func"]
        if fn == "nz":
            if len(ast_node["args"]) != 1:
                return None
            v = evaluate(ast_node["args"][0], data)
            return 0.0 if v is None else v
        args = [evaluate(a, data) for a in ast_node["args"]]
        if any(a is None for a in args):
            return None
        if fn == "abs" and len(args) == 1:
            return abs(args[0])
        if fn == "sum":
            return sum(args)
        if fn == "avg":
            return sum(args) / len(args) if args else None
        if fn == "yoy" and len(args) == 2:
            cur, prev = args
            return None if prev == 0 else (cur - prev) / abs(prev)
    return None


def eval_text(data: dict, formula: str):
    """在 data 上求值公式字符串；语法错误返回 None。"""
    try:
        return evaluate(parse_formula(formula), data)
    except FormulaError:
        return None
