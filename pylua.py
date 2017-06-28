import io
import sys
import os
import ast
import re
import subprocess

lua_exe = '~/src/luajit-2.0/src/luajit'
lua_exe = os.path.normpath(os.path.expanduser(lua_exe))

# a modified version of the function from ast.py, with an optional "whitespace"
# argument
def dump(node, annotate_fields=True, include_attributes=False, whitespace=False):
    """
    Return a formatted dump of the tree in *node*.  This is mainly useful for
    debugging purposes.  The returned string will show the names and the values
    for fields.  This makes the code impossible to evaluate, so if evaluation is
    wanted *annotate_fields* must be set to False.  Attributes such as line
    numbers and column offsets are not dumped by default.  If this is wanted,
    *include_attributes* can be set to True.
    """
    def _format(node, indent=0):
        sp = ('  ' * (indent+1)) if whitespace else ''
        nl = '\n' if whitespace else ''

        if isinstance(node, ast.AST):
            fields = [(a, _format(b, indent+1)) for a, b in ast.iter_fields(node)]
            rv = '%s(%s%s%s' % (node.__class__.__name__, nl, sp, ', '.join(
                ('%s=%s' % field for field in fields)
                if annotate_fields else
                (b for a, b in fields)
            ))
            if include_attributes and node._attributes:
                rv += fields and ', ' or ' '
                rv += ', '.join('%s=%s' % (a, _format(getattr(node, a), indent+1))
                                for a in node._attributes)
            return rv + ')'
        elif isinstance(node, list):
            return '[%s%s%s]' % (nl, sp, ', '.join(_format(x, indent+1) for x in node))
        return repr(node)
    if not isinstance(node, ast.AST):
        raise TypeError('expected AST, got %r' % node.__class__.__name__)
    return _format(node)

class PyLua(ast.NodeVisitor):
    def __init__(self):
        self.stream = io.StringIO()
        self.indentation = 0
        # variable name scopes (environments); FIXME: leaky heuristic
        self.envs = [{}]
        # indentation levels where '::continue::' is wanted; FIXME: leaky heuristic
        self.wantcontinue = set()
        # packages, and other stuff which doesn't require ':' calling convention
        self.nocolon = set()

    def visit_all(self, nodes):
        for node in nodes:
            self.visit(node)

    def visit_all_sep(self, nodes, sep):
        first = True
        for node in nodes:
            if first:
                first = False
            else:
                self.emit(sep)
            self.visit(node)

    def visit_or(self, node, orelse):
        if node:
            self.visit(node)
        else:
            self.emit(orelse)

    def visit(self, node):
        super(PyLua, self).visit(node)

    def visit_Print(self, node):
        self.emit('print(')
        self.generic_visit(node)
        self.emit(')')

    def visit_Num(self, node):
        self.emit(repr(node.n))

    def visit_Add(self, node):
        self.emit('+')
    def visit_Mult(self, node):
        self.emit('*')
    def visit_Div(self, node):
        self.emit('/')
    def visit_Sub(self, node):
        self.emit('-')

    def visit_Return(self, node):
        self.indent()
        self.emit('return ')
        self.generic_visit(node)
        self.eol()

    def visit_FunctionDef(self, node):
        v = dict(body='foo')
        v.update(**vars(node))

        self.emit('\n')

        self.env_push()
        self.indent()
        self.emit('%(name)s = function(' % v)
        self.visit(node.args)
        self.emit(')\n')

        self.push_scope()
        default0 = len(node.args.args)-len(node.args.defaults)
        for i, default in enumerate(node.args.defaults):
            if isinstance(default, ast.Name) and default.id=='None':
                continue
            arg = node.args.args[default0+i]
            self.indent()
            self.visit(arg)
            self.emit(' = ')
            self.visit(arg)
            self.emit(' or ')
            self.visit(default)
            self.eol()
        self.visit_all(node.body)
        self.pop_scope()

        #self.emit('\n')
        self.indent()
        self.emit('end\n')
        self.env_pop()

    def visit_Lambda(self, node):
        self.emit('function(')
        # TODO: instead of node.args.args, create and use common method visit_arguments ?
        self.visit_all_sep(node.args.args, ', ')
        self.emit(') return ')
        self.visit(node.body)
        self.emit(' end')

    ident_re = re.compile(r'^[A-Za-z_][\w_]*$')

    def visit_Dict(self, node):
        self.emit('{ ')
        for k,v in zip(node.keys, node.values):
            if isinstance(k, ast.Str) and self.ident_re.match(k.s):
                # optimize pretty keys
                self.emit(k.s)
            else:
                self.emit('[')
                self.visit(k)
                self.emit(']')
            self.emit('=')
            self.visit(v)
            self.emit(', ')
        self.emit('}')

    def visit_List(self, node):
        self.emit('{')
        self.visit_all_sep(node.elts, ', ')
        self.emit('}')

    def visit_arg(self, node):
        self.emit(node.arg)

    def visit_arguments(self, node):
        self.visit_all_sep(node.args, ', ')
        # FIXME: kwargs, ...

    def visit_Print(self, node):
        self.indent()
        self.emit('PYLUA.print(')
        self.visit_all_sep(node.values, ', ')
        if node.nl:
            if len(node.values)>0:
                self.emit(', ')
            self.emit(r"'\n'")
        self.emit(')\n')

    def visit_TryExcept(self, node):
        self.indent()
        self.emit('-- PYLUA.FIXME: TRY:\n')

        #self.push_scope()
        self.visit_all(node.body)
        #self.pop_scope()

        for x in node.handlers:
            if isinstance(x, ast.ExceptHandler):
                self.indent()
                self.emit('-- PYLUA.FIXME: EXCEPT ')
                self.visit(x.type)
                if x.name:
                    self.emit(' ')
                    self.visit(x.name)
                self.emit(':\n')

                self.push_scope()
                self.visit_all(x.body)
                self.pop_scope()
            else:
                self.indent()
                self.emit('-- PYLUA.FIXME: '+x.__class__.__name__)
                self.eol()

        if len(node.orelse)>0:
            self.indent()
            self.emit('-- PYLUA.FIXME: FINALLY:\n')

            self.push_scope()
            self.visit_all(node.orelse)
            self.pop_scope()

    def visit_BinOp(self, node):
        if isinstance(node.op, ast.Pow):
            self.emit('math.pow(')
            self.visit(node.left)
            self.emit(', ')
            self.visit(node.right)
            self.emit(')')
        elif isinstance(node.op, ast.Mod) and isinstance(node.left, ast.Str):
            self.emit('string.format(')
            self.visit(node.left)
            self.emit(', ')
            if isinstance(node.right, ast.Tuple):
                self.visit_all_sep(node.right.elts, ', ')
            else:
                self.visit(node.right)
            self.emit(')')
        elif isinstance(node.op, ast.Mod):
            self.emit('(')
            self.visit(node.left)
            self.emit('%')
            self.visit(node.right)
            self.emit(')')
        elif isinstance(node.op, ast.FloorDiv):
            self.emit('(')
            self.visit(node.left)
            self.emit('//')
            self.visit(node.right)
            self.emit(')')
        elif isinstance(node.op, ast.Add) and isinstance(node.left, ast.Str):
            self.visit(node.left)
            self.emit(' .. ')
            self.visit(node.right)
        else:
            self.emit_paren_maybe(node, node.left, '(')
            self.visit(node.left)
            self.emit_paren_maybe(node, node.left, ')')
            self.visit(node.op)
            self.emit_paren_maybe(node, node.right, '(', True)
            self.visit(node.right)
            self.emit_paren_maybe(node, node.right, ')', True)

    def visit_BoolOp(self, node):
        first = True
        for x in node.values:
            if first:
                first = False
            else:
                self.visit(node.op)
            self.emit_paren_maybe(node, x, '(')
            self.visit(x)
            self.emit_paren_maybe(node, x, ')')

    def visit_UnaryOp(self, node):
        self.visit(node.op)
        self.emit_paren_maybe(node, node.operand, '(')
        self.visit(node.operand)
        self.emit_paren_maybe(node, node.operand, ')')

    def visit_Not(self, node):
        self.emit(' not ')
    def visit_USub(self, node):
        self.emit('-')

    def visit_IfExp(self, node):
        # FIXME here and in similar: resolve parentheses and priorities!
        self.visit(node.test)
        self.emit(' and ')
        self.visit(node.body)
        self.emit(' or ')
        self.visit(node.orelse)

    def visit_Call(self, node):
        if isinstance(node.func, ast.Attribute) and node.func.attr == 'append':
            self.emit('table.insert(')
            self.visit(node.func.value)
            self.emit(', ')
            self.visit_all_sep(node.args, ', ')
            self.emit(')')
            return
        if isinstance(node.func, ast.Attribute) and node.func.attr == 'join' and \
                isinstance(node.func.value, ast.Str) and len(node.args)==1:
            arg = node.args[0]
            if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Attribute) and \
                    arg.func.attr == 'split' and len(arg.args)==0:
                # ' '.join(sss.split())
                self.emit('string.gsub(')
                self.visit(arg.func.value)
                self.emit(", '%s+', ")
                self.visit(node.func.value)
                self.emit(')')
                return
            self.emit('table.concat(')
            self.visit(node.args[0])
            self.emit(', ')
            self.visit(node.func.value)
            self.emit(')')
            return
        if isinstance(node.func, ast.Attribute) and \
                node.func.attr == "lower":
            self.emit('string.')
            self.emit(node.func.attr)
            self.emit('(')
            self.visit(node.func.value)
            if len(node.keywords)>0:
                self.emit(', PYLUA.keywords{')
                self.visit_all_sep(node.keywords, ', ')
                self.emit('}')
            if len(node.args)>0:
                self.emit(', ')
                self.visit_all_sep(node.args, ', ')
            self.emit(')')
            return
        if isinstance(node.func, ast.Attribute) and \
                node.func.attr in ['keys', 'replace', 'split', 'update', 'copy',
                                   'endswith', 'find', 'lower', 'setdefault', 'strip',
                                   'startswith', 'join', 'items', 'sort']:
            self.emit('PYLUA.')
            self.emit(node.func.attr)
            self.emit('(')
            self.visit(node.func.value)
            if len(node.keywords)>0:
                self.emit(', PYLUA.keywords{')
                self.visit_all_sep(node.keywords, ', ')
                self.emit('}')
            if len(node.args)>0:
                self.emit(', ')
                self.visit_all_sep(node.args, ', ')
            self.emit(')')
            return
        if isinstance(node.func, ast.Attribute) and node.func.attr == 'get' and \
                len(node.args)>=1 and len(node.args)<=2:
            self.visit(node.func.value)
            self.emit('[')
            if isinstance(node.args[0], ast.Tuple):
                self.emit('PYLUA.keytuple')
            self.visit(node.args[0])
            self.emit(']')
            if len(node.args)==2:
                self.emit(' or ')
                self.visit(node.args[1])
            return
        if isinstance(node.func, ast.Name) and node.func.id == 'len' and len(node.args)==1:
            noparen = isinstance(node.args[0], ast.Attribute) or isinstance(node.args[0], ast.Name)
            self.emit('#')
            if not noparen: self.emit('(')
            self.visit(node.args[0])
            if not noparen: self.emit(')')
            return
        stdfuncs = {'max':'math.max', 'min':'math.min', 'ord':'PYLUA.ord', 'str':'tostring',
                    'map':'PYLUA.map', 'sum':'PYLUA.sum', 'open':'PYLUA.open'}
        if isinstance(node.func, ast.Name) and node.func.id in list(stdfuncs.keys()):
            self.emit(stdfuncs[node.func.id])
            self.emit('(')
            self.visit_all_sep(node.args, ', ')
            self.emit(')')
            return
        if isinstance(node.func, ast.Attribute) and \
                ((not isinstance(node.func.value, ast.Name)) or node.func.value.id not in self.nocolon):
            self.visit(node.func.value)
            self.emit('.')
            self.emit(node.func.attr)
        else:
            self.visit(node.func)
        self.emit('(')
        first = True
        if len(node.keywords)>0:
            first = False
            self.emit('PYLUA.keywords{')
            self.visit_all_sep(node.keywords, ', ')
            self.emit('}')
        if len(node.args)>0:
            if first:
                first = False
            else:
                self.emit(', ')
            self.visit_all_sep(node.args, ', ')
        self.emit(')')

    def visit_keyword(self, node):
        self.emit(node.arg)
        self.emit('=')
        self.visit(node.value)

    def visit_Compare(self, node):
        self.visit(node.left)
        self.visit_all(node.ops)
        self.visit_all(node.comparators)

    def visit_Subscript(self, node):
        if isinstance(node.slice, ast.Index):
            self.visit(node.value)
            self.emit('[')
            if isinstance(node.slice.value, ast.Num):
                self.emit('%d' % (node.slice.value.n + 1))
            elif isinstance(node.slice.value, ast.Tuple):
                self.emit('PYLUA.keytuple')
                self.visit(node.slice)
            else:
                self.visit(node.slice)
            self.emit(']')
        elif isinstance(node.slice, ast.Slice):
            # TODO: PYLUA.slice because other for string vs. table
            self.emit('PYLUA.slice(')
            self.visit(node.value)
            self.emit(', ')
            self.visit_or(node.slice.lower, 'nil')
            self.emit(', ')
            self.visit_or(node.slice.upper, 'nil')
            if node.slice.step:
                self.emit(', ')
                self.visit(node.step)
            self.emit(')')
        else:
            self.emit('[ ? ]')

    def visit_Tuple(self, node):
        self.emit('{')
        self.visit_all_sep(node.elts, ', ')
        self.emit('}')

    def visit_Name(self, node):
        self.emit(node.id)
        self.env_add(node.id)

    def visit_Assign(self, node):
        self.indent()
        if len(node.targets)==1 and isinstance(node.targets[0], ast.Tuple):
            newlocals = []
            for x in node.targets[0].elts:
                if isinstance(x, ast.Name) and not self.env_has(x.id):
                    newlocals.append(x.id)
            if len(newlocals) == len(node.targets[0].elts):
                # is it global or local? 
                if self.indentation > 0:
                    self.emit('local ')
            elif len(newlocals)>0:
                # is it global or local? 
                if self.indentation > 0:
                    self.emit('local ')
                self.emit(', '.join(newlocals))
                self.eol()
                self.indent()
            self.visit_all_sep(node.targets[0].elts, ', ')
            self.emit(' = unpack(')
            self.visit(node.value)
            self.emit(')\n')
        elif len(node.targets) > 1:
            # TODO: can there be >1 targets? what's difference with 1 target, a Tuple?
            self.emit('-- PYLUA.FIXME Assign\n')
        else:
            x = node.targets[0]
            if isinstance(x, ast.Name) and not self.env_has(x.id):
                # is it global or local? 
                if self.indentation > 0:
                    self.emit('local ')
            self.visit(x)
            self.emit(' = ')
            self.visit(node.value)
            self.eol()

    def visit_AugAssign(self, node):
        self.indent()
        self.visit(node.target)
        self.emit(' = ')
        self.visit(node.target)
        fakeParent = ast.BinOp(node.value, node.op, node.value)
        self.visit(node.op)
        self.emit_paren_maybe(fakeParent, node.value, '(', True)
        self.visit(node.value)
        self.emit_paren_maybe(fakeParent, node.value, ')', True)
        self.eol()

    def visit_Expr(self, node):
        if isinstance(node.value, ast.Str):
            for line in node.value.s.splitlines():
                self.indent()
                self.emit('-- ')
                self.emit(line)
                self.eol()
        else:
            self.indent()
            self.visit(node.value)
            self.eol()  # TODO: yes, or no?

    def visit_Import(self, node):
        for x in node.names:
            if isinstance(x, ast.alias):
                self.indent()
                self.emit('local ')
                if x.asname:
                    self.emit(x.asname)
                    self.nocolon.add(x.asname)
                else:
                    self.emit(x.name)
                    self.nocolon.add(x.name)
                self.emit(" = require('")
                self.emit(x.name)
                self.emit("')\n")
            else:
                self.emit("-- FIXME: "+x.__class__.__name__)

    def visit_ImportFrom(self, node):
        for x in node.names:
            if isinstance(x, ast.alias):
                self.indent()
                self.emit('local ')
                if x.asname:
                    self.emit(x.asname)
                    self.nocolon.add(x.asname)
                else:
                    self.emit(x.name)
                    self.nocolon.add(x.name)
                self.emit(" = require('")
                self.emit(node.module)
                self.emit("').")
                self.emit(x.name)
                self.eol()
            else:
                self.emit("-- FIXME: "+x.__class__.__name__)

    def visit_ClassDef(self, node):
        self.eol()
        self.indent()
        self.emit(node.name)
        self.emit(' = PYLUA.class(')
        self.visit_all_sep(node.bases, ', ')
        self.emit(') {\n')

        self.push_scope()
        for x in node.body:
            if isinstance(x, ast.Expr):
                self.visit(x)
            elif isinstance(x, ast.FunctionDef):
                self.visit(x)
                self.indent()
                self.emit(';\n')
            else:
                self.emit('-- PYLUA.FIXME ast.'+x.__class__.__name__)
                self.eol()
        self.pop_scope()

        self.emit('}\n\n')

    def visit_Raise(self, node):
        self.indent()
        self.emit('error(')
        self.visit(node.type)
        if node.inst is not None:  # TODO: is this correct condition?
            self.emit('(')
            if isinstance(node.inst, ast.Tuple):  # TODO: ok? or not?
                self.visit_all_sep(node.inst.elts, ', ')
            else:
                self.visit(node.inst)
            self.emit(')')
        self.emit(')\n')

    def visit_If(self, node):
        self.indent()
        self.emit('if ')
        def test_plus_body(self, node):
            self.visit(node.test)
            self.emit(' then\n')

            self.push_scope()
            self.visit_all(node.body)
            self.pop_scope()

            if node.orelse:
                if len(node.orelse)==1 and isinstance(node.orelse[0], ast.If):
                    # optimize elif into 'elseif'
                    self.indent()
                    self.emit('elseif ')
                    test_plus_body(self, node.orelse[0])
                else:
                    self.indent()
                    self.emit('else\n')
                    self.push_scope()
                    self.visit_all(node.orelse)
                    self.pop_scope()
        test_plus_body(self, node)

        self.indent()
        self.emit('end\n')

    def visit_While(self, node):
        self.indent()
        self.emit('while ')
        self.visit(node.test)
        self.emit(' do\n')

        self.push_scope()
        self.visit_all(node.body)
        wantcontinue = {i for i in self.wantcontinue if i>=self.indentation}
        if len(wantcontinue) > 0:
            self.indent()
            self.emit('::continue::\n')
            self.wantcontinue -= wantcontinue
            assert 0, "continue is not in Lua."
        self.pop_scope()

        self.indent()
        self.emit('end\n')

    def visit_Break(self, node):
        self.indent()
        self.emit('break\n')

    def visit_For(self, node):
        self.env_push()  # TODO: is this correct?
        self.indent()
        ituple = None
        if node.target and node.iter and \
                isinstance(node.iter, ast.Call) and \
                isinstance(node.iter.func, ast.Attribute) and \
                node.iter.func.attr == 'items':
            # Python: for k,v in dict.items():
            self.emit('for ')
            if isinstance(node.target, ast.Tuple):
                self.visit_all_sep(node.target.elts, ', ')
            else:
                self.visit(node.target)
            self.emit(' in pairs(')
            self.visit(node.iter.func.value)
            self.emit(') do\n')
        # TODO: for c in range(len(tab)):  --> for c = 1,#tab:
        # TODO: for c in range(a, b):      --> for c = a,b-1: ???
        elif node.target and node.iter:
            self.emit('for _, ')
            if isinstance(node.target, ast.Tuple):
                ituple = node.target
                self.emit('PYLUA_x')
            else:
                self.visit(node.target)
            self.emit(' in ipairs(')
            self.visit(node.iter)
            self.emit(') do\n')
        else:
            self.emit('PYLUA.FOR ... ?\n')

        self.push_scope()
        if ituple:
            self.indent()
            self.emit('local ')
            self.visit_all_sep(ituple.elts, ', ')
            self.emit(' = unpack(PYLUA_x)\n')
        self.visit_all(node.body)
        wantcontinue = {i for i in self.wantcontinue if i>=self.indentation}
        if len(wantcontinue) > 0:
            self.indent()
            self.emit('::continue::\n')
            self.wantcontinue -= wantcontinue
            assert 0, "continue is not in Lua."
        self.pop_scope()

        self.indent()
        self.emit('end\n')
        self.env_pop()

        if len(node.orelse)>0:
            self.indent()
            self.emit('-- PYLUA.FIXME: else:\n')

            self.push_scope()
            self.visit_all(node.orelse)
            self.pop_scope()

    def visit_Continue(self, node):
        self.indent()
        self.emit('goto continue\n')
        # FIXME: very rough heuristic
        self.wantcontinue.add(self.indentation-1)
        assert 0, "Continue is non-existent in Lua!"

    def visit_ListComp(self, node):
        if len(node.generators)>1 or len(node.generators[0].ifs)>1:
            self.emit('PYLUA.COMPREHENSION()')
            return
        gen = node.generators[0]
        self.emit('PYLUA.collect(')
        self.visit(gen.iter)
        self.emit(', function(')
        if not isinstance(gen.target, ast.Name):
            self.emit('--[[PYLUA.FIXME: ListComp]] ')
        self.visit(gen.target)
        self.emit(') ')
        if len(gen.ifs) > 0:
            self.emit('if ')
            self.visit(gen.ifs[0])
            self.emit(' then ')
        self.emit('return ')
        self.visit(node.elt)
        if len(gen.ifs) > 0:
            self.emit(' end')
        self.emit(' end)')

    def visit_Compare(self, node):
        if len(node.ops)==1 and isinstance(node.ops[0], ast.NotIn):
            self.visit_all_sep(node.comparators, ', ')
            self.emit('[')
            self.visit(node.left)
            self.emit('] == nil')
        elif len(node.ops)==1 and isinstance(node.ops[0], ast.In):
            if len(node.comparators)==1 and isinstance(node.comparators[0], ast.Attribute) and \
                    node.comparators[0].attr == 'keys':
                # x in y.keys() --> y[x]
                self.visit(node.comparators[0])
                self.emit('[')
                self.visit(node.left)
                self.emit(']')
                return
            self.visit_all_sep(node.comparators, ', ')
            self.emit('[')
            self.visit(node.left)
            self.emit('] ~= nil')
        elif len(node.ops)==1 and isinstance(node.ops[0], ast.Is):
            if len(node.comparators)==1 and isinstance(node.comparators[0], ast.Name) and \
                    node.comparators[0].id == 'None':
                self.visit(node.left)
                self.emit(' == nil')
                return
            self.emit('PYLUA.op_is(')
            self.visit(node.left)
            self.emit(', ')
            self.visit_all_sep(node.comparators, ', ')
            self.emit(')')
        elif len(node.ops)==1 and isinstance(node.ops[0], ast.IsNot):
            if len(node.comparators)==1 and isinstance(node.comparators[0], ast.Name) and \
                    node.comparators[0].id == 'None':
                self.visit(node.left)
                self.emit(' ~= nil')
                return
            self.emit('PYLUA.op_is_not(')
            self.visit(node.left)
            self.emit(', ')
            self.visit_all_sep(node.comparators, ', ')
            self.emit(')')
        else:
            self.visit(node.left)
            self.visit_all(node.ops)
            self.visit_all(node.comparators)

    def visit_Lt(self, node):
        self.emit('<')
    def visit_LtE(self, node):
        self.emit('<=')
    def visit_Gt(self, node):
        self.emit('>')
    def visit_GtE(self, node):
        self.emit('>=')
    def visit_Eq(self, node):
        self.emit('==')
    def visit_NotEq(self, node):
        self.emit('~=')

    def visit_And(self, node):
        self.emit(' and ')
    def visit_Or(self, node):
        self.emit(' or ')

    def visit_NameConstant(self, node):
        if node.value == None:
            self.emit('nil')
        elif node.value == True:
            self.emit('true')
        elif node.value == False:
            self.emit('false')
        else:
            assert 0, "Unknown NameConstant"

    def visit_Attribute(self, node):
        self.visit(node.value)
        self.emit('.')
        self.emit(node.attr)

    def visit_Str(self, node):
        # TODO: prettier multiline strings (but must not have escape sequences other than \n)
        self.emit("'")
        # FIXME: better escaping of strings
        self.emit(node.s)
        #self.emit(node.s.replace('\\', '\\\\').replace('"', '\\"'))
        self.emit("'")

    def push_scope(self):
        self.indentation += 1
    def pop_scope(self):
        self.indentation -= 1

    def emit_paren_maybe(self, parent, child, text, right=False):
        if isinstance(parent, ast.BinOp) and isinstance(child, ast.BinOp) and \
                (isinstance(parent.op, ast.Mult) or isinstance(parent.op, ast.Div)) and \
                (isinstance(child.op, ast.Add) or isinstance(child.op, ast.Sub)):
            self.emit(text)  # (..+..) / (..-..)   (..+..) * (..-..)
            return
        if right and isinstance(parent, ast.BinOp) and isinstance(child, ast.BinOp) and \
                isinstance(parent.op, ast.Sub) and \
                (isinstance(child.op, ast.Sub) or isinstance(child.op, ast.Add)):
            self.emit(text)  # .. - (..+..)   .. - (..-..)
            return
        if isinstance(parent, ast.BinOp) and isinstance(child, ast.BoolOp):
            self.emit(text)
            return
        if isinstance(parent, ast.BoolOp) and isinstance(child, ast.BoolOp) and \
                isinstance(parent.op, ast.And) and isinstance(child.op, ast.Or):
            self.emit(text)  # (..or..) and ...
            return
        if isinstance(parent, ast.UnaryOp) and isinstance(parent.op, ast.Not) and \
                isinstance(child, ast.BoolOp):
            self.emit(text)
            return
        if isinstance(parent, ast.UnaryOp) and isinstance(parent.op, ast.USub) and \
                isinstance(child, ast.BinOp) and \
                (isinstance(child.op, ast.Add) or isinstance(child.op, ast.Sub)):
            self.emit(text)  # -(..+..)   -(..-..)
            return

    def indent(self):
        self.emit('  '*self.indentation)
    def eol(self):
        self.emit('\n')

    def emit(self, val):
        self.stream.write(val)

    def env_push(self):
        self.envs.append({})
    def env_pop(self):
        self.envs.pop()
    def env_add(self, name):
        self.envs[len(self.envs)-1][name] = True
    def env_has(self, name):
        for env in self.envs:
            if name in env:
                return True
        return False

_dump_ast=dump
def run_file(filename, dump=False):
    contents = open(filename, 'rU').read()
    if not contents.endswith('\n'):
        contents += '\n'

    tree = ast.parse(contents, filename)

    visitor = PyLua()
    visitor.visit(tree)

    lua_program = visitor.stream.getvalue()
    if dump:
        print(_dump_ast(tree, include_attributes=True, whitespace=True))
    #    print '-'*80
    #    print lua_program
    #    print '-'*80
    #else:
    #    return runjit(lua_program)
    return runjit(lua_program)

def main():
    filename = sys.argv[1]
    print(run_file(filename, True))

def runjit(program):
    filename = '_pylua_temp.lua'
    open(filename, 'w').write(program)
    #try:
    #    args = [lua_exe, filename]
    #    process = subprocess.Popen(args, stdout = subprocess.PIPE)
    #    stdout, stderr = process.communicate()
    #finally:
    #    os.remove(filename)

    #return stdout

if __name__ == '__main__':
    main()

