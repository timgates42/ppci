"""
    This module contains the code generation class.
"""

import logging
import struct
from .. import ir
from .. import irutils
from . import astnodes as ast
from .scope import SemanticError


def pack_string(txt):
    """ Pack a string using 4 bytes length followed by text data """
    # TODO: this is probably machine depending?
    length = struct.pack('<I', len(txt))
    data = txt.encode('ascii')
    return length + data


class Analyzer:
    """ Type checker and other constraints """
    def check_module(self, mod, context):
        pass

    def check_binop(self, expr):
        pass


class CodeGenerator:
    """
      Generates intermediate (IR) code from a package. The entry function is
      'genModule'. The main task of this part is to rewrite complex control
      structures, such as while and for loops into simple conditional
      jump statements. Also complex conditional statements are simplified.
      Such as 'and' and 'or' statements are rewritten in conditional jumps.
      And structured datatypes are rewritten.

      Type checking is done in one run with code generation.
    """
    def __init__(self, diag):
        self.logger = logging.getLogger('c3cgen')
        self.builder = irutils.Builder()
        self.diag = diag
        self.context = None
        self.analyzer = Analyzer()

    def emit(self, instruction):
        """
            Emits the given instruction to the builder.
            Can be muted for constants.
        """
        self.builder.emit(instruction)
        return instruction

    def gencode(self, mod, context):
        """ Generate code for a single module """
        assert type(mod) is ast.Module
        self.context = context
        self.builder.prepare()
        self.ok = True
        self.logger.debug('Generating ir-code for {}'.format(mod.name))
        self.varMap = {}    # Maps variables to storage locations.
        self.builder.m = ir.Module(mod.name)
        try:
            for typ in mod.types:
                self.context.check_type(typ)
            # Only generate function if function contains a body:
            real_functions = list(filter(
                lambda f: f.body, mod.functions))
            # Generate room for global variables:
            for var in mod.innerScope.variables:
                ir_var = ir.Variable(var.name, self.context.size_of(var.typ))
                self.varMap[var] = ir_var
                assert not var.isLocal
                self.builder.m.add_variable(ir_var)
            for func in real_functions:
                self.gen_function(func)
        except SemanticError as ex:
            self.error(ex.msg, ex.loc)
        if not self.ok:
            raise SemanticError("Errors occurred", None)
        return self.builder.m

    def error(self, msg, loc=None):
        """ Emit error to diagnostic system and mark package as invalid """
        self.ok = False
        self.diag.error(msg, loc)

    def gen_function(self, function):
        """ Generate code for a function. This involves creating room
            for parameters on the stack, and generating code for the function
            body.
        """
        # TODO: handle arguments
        ir_function = self.builder.new_function(function.name)
        self.builder.setFunction(ir_function)
        first_block = self.builder.newBlock()
        self.emit(ir.Jump(first_block))
        self.builder.setBlock(first_block)

        # generate room for locals:
        for sym in function.innerScope:
            self.context.check_type(sym.typ)
            var_name = 'var_{}'.format(sym.name)
            variable = ir.Alloc(var_name, self.context.size_of(sym.typ))
            self.emit(variable)
            if sym.isParameter:
                # TODO: parameters are now always integers?

                # Define parameter for function:
                parameter = ir.Parameter(sym.name, ir.i32)
                ir_function.add_parameter(parameter)

                # For paramaters, allocate space and copy the value into
                # memory. Later, the mem2reg pass will extract these values.
                # Move parameter into local copy:
                self.emit(ir.Store(parameter, variable))
            elif isinstance(sym, ast.Variable):
                pass
            else:
                raise NotImplementedError('{}'.format(sym))
            self.varMap[sym] = variable

        self.gen_stmt(function.body)
        # self.emit(ir.Move(f.return_value, ir.Const(0)))
        self.emit(ir.Jump(ir_function.epiloog))
        self.builder.setBlock(ir_function.epiloog)
        self.builder.setFunction(None)

    def get_ir_type(self, cty, loc):
        """ Given a certain type, get the corresponding ir-type """
        cty = self.context.the_type(cty)
        if self.context.equal_types(cty, self.context.intType):
            return ir.i32
        elif self.context.equal_types(cty, self.context.doubleType):
            # TODO: implement true floating point.
            return ir.i32
        elif self.context.equal_types(cty, self.context.boolType):
            # Implement booleans as integers:
            return ir.i32
        elif self.context.equal_types(cty, self.context.byteType):
            return ir.i8
        elif isinstance(cty, ast.PointerType):
            return ir.ptr
        else:
            raise SemanticError(
                'Cannot determine the load type for {}'.format(cty), loc)

    def gen_stmt(self, code):
        """ Generate code for a statement """
        try:
            assert isinstance(code, ast.Statement)
            self.builder.setLoc(code.loc)
            if type(code) is ast.Compound:
                for statement in code.statements:
                    self.gen_stmt(statement)
            elif type(code) is ast.Empty:
                pass
            elif type(code) is ast.Assignment:
                self.gen_assignment_stmt(code)
            elif type(code) is ast.ExpressionStatement:
                self.gen_expr_code(code.ex)
                # Check that this is always a void function call
                if not isinstance(code.ex, ast.FunctionCall):
                    raise SemanticError('Not a call expression', code.ex.loc)
            elif type(code) is ast.If:
                self.gen_if_stmt(code)
            elif type(code) is ast.Return:
                self.gen_return_stmt(code)
            elif type(code) is ast.While:
                self.gen_while(code)
            elif type(code) is ast.For:
                self.gen_for_stmt(code)
            else:
                raise NotImplementedError('Unknown stmt {}'.format(code))
        except SemanticError as exc:
            self.error(exc.msg, exc.loc)

    def gen_return_stmt(self, code):
        """ Generate code for return statement """
        ret_val = self.make_rvalue_expr(code.expr)
        self.emit(ir.Return(ret_val))
        block = self.builder.newBlock()
        self.builder.setBlock(block)

    def do_coerce(self, ir_val, typ, wanted_typ, loc):
        """ Try to convert expression into the given type
            ir_val: the value to convert
            typ: the type of the value
            wanted_typ: the type that it must be
            loc: the location where this is needed.
            Raises an error is the conversion cannot be done.
        """
        if self.context.equal_types(typ, wanted_typ):
            # no cast required
            return ir_val
        elif self.context.equal_types(self.context.intType, typ) and \
                isinstance(wanted_typ, ast.PointerType):
            return self.emit(ir.IntToPtr(ir_val, 'coerce'))
        else:
            raise SemanticError(
                "Cannot use {} as {}".format(typ, wanted_typ), loc)

    def gen_assignment_stmt(self, code):
        """ Generate code for assignment statement """
        lval = self.gen_expr_code(code.lval)
        rval = self.make_rvalue_expr(code.rval)
        rval = self.do_coerce(rval, code.rval.typ, code.lval.typ, code.loc)
        if not code.lval.lvalue:
            raise SemanticError(
                'No valid lvalue {}'.format(code.lval), code.lval.loc)
        # TODO: for now treat all stores as volatile..
        # TODO: determine volatile properties from type??
        volatile = True
        return self.emit(ir.Store(rval, lval, volatile=volatile))

    def gen_if_stmt(self, code):
        """ Generate code for if statement """
        true_block = self.builder.newBlock()
        bbfalse = self.builder.newBlock()
        final_block = self.builder.newBlock()
        self.gen_cond_code(code.condition, true_block, bbfalse)
        self.builder.setBlock(true_block)
        self.gen_stmt(code.truestatement)
        self.emit(ir.Jump(final_block))
        self.builder.setBlock(bbfalse)
        self.gen_stmt(code.falsestatement)
        self.emit(ir.Jump(final_block))
        self.builder.setBlock(final_block)

    def gen_while(self, code):
        """ Generate code for while statement """
        bbdo = self.builder.newBlock()
        test_block = self.builder.newBlock()
        final_block = self.builder.newBlock()
        self.emit(ir.Jump(test_block))
        self.builder.setBlock(test_block)
        self.gen_cond_code(code.condition, bbdo, final_block)
        self.builder.setBlock(bbdo)
        self.gen_stmt(code.statement)
        self.emit(ir.Jump(test_block))
        self.builder.setBlock(final_block)

    def gen_for_stmt(self, code):
        """ Generate for-loop code """
        bbdo = self.builder.newBlock()
        test_block = self.builder.newBlock()
        final_block = self.builder.newBlock()
        self.gen_stmt(code.init)
        self.emit(ir.Jump(test_block))
        self.builder.setBlock(test_block)
        self.gen_cond_code(code.condition, bbdo, final_block)
        self.builder.setBlock(bbdo)
        self.gen_stmt(code.statement)
        self.gen_stmt(code.final)
        self.emit(ir.Jump(test_block))
        self.builder.setBlock(final_block)

    def gen_cond_code(self, expr, bbtrue, bbfalse):
        """ Generate conditional logic.
            Implement sequential logical operators. """
        if type(expr) is ast.Binop:
            if expr.op == 'or':
                l2 = self.builder.newBlock()
                self.gen_cond_code(expr.a, bbtrue, l2)
                if not self.context.equal_types(expr.a.typ, self.context.boolType):
                    raise SemanticError('Must be boolean', expr.a.loc)
                self.builder.setBlock(l2)
                self.gen_cond_code(expr.b, bbtrue, bbfalse)
                if not self.context.equal_types(expr.b.typ, self.context.boolType):
                    raise SemanticError('Must be boolean', expr.b.loc)
            elif expr.op == 'and':
                l2 = self.builder.newBlock()
                self.gen_cond_code(expr.a, l2, bbfalse)
                if not self.context.equal_types(expr.a.typ, self.context.boolType):
                    self.error('Must be boolean', expr.a.loc)
                self.builder.setBlock(l2)
                self.gen_cond_code(expr.b, bbtrue, bbfalse)
                if not self.context.equal_types(expr.b.typ, self.context.boolType):
                    raise SemanticError('Must be boolean', expr.b.loc)
            elif expr.op in ['==', '>', '<', '!=', '<=', '>=']:
                ta = self.make_rvalue_expr(expr.a)
                tb = self.make_rvalue_expr(expr.b)
                if not self.context.equal_types(expr.a.typ, expr.b.typ):
                    raise SemanticError('Types unequal {} != {}'
                                        .format(expr.a.typ, expr.b.typ),
                                        expr.loc)
                self.emit(ir.CJump(ta, expr.op, tb, bbtrue, bbfalse))
            else:
                raise SemanticError('non-bool: {}'.format(expr.op), expr.loc)
            expr.typ = self.context.boolType
        elif type(expr) is ast.Literal:
            self.gen_expr_code(expr)
            if expr.val:
                self.emit(ir.Jump(bbtrue))
            else:
                self.emit(ir.Jump(bbfalse))
        else:
            raise NotImplementedError('Unknown cond {}'.format(expr))

        # Check that the condition is a boolean value:
        if not self.context.equal_types(expr.typ, self.context.boolType):
            self.error('Condition must be boolean', expr.loc)

    def make_rvalue_expr(self, expr):
        """ Generate expression code and insert an extra load instruction
            when required.
            This means that the value can be used in an expression or as
            a parameter.
        """
        value = self.gen_expr_code(expr)
        if expr.lvalue:
            # Determine loaded type:
            load_ty = self.get_ir_type(expr.typ, expr.loc)

            # Load the value:
            return self.emit(ir.Load(value, 'loaded', load_ty))
        else:
            # The value is already an rvalue:
            return value

    def gen_expr_code(self, expr):
        """ Generate code for an expression. Return the generated ir-value """
        assert isinstance(expr, ast.Expression)
        if type(expr) is ast.Binop:
            return self.gen_binop(expr)
        elif type(expr) is ast.Unop:
            if expr.op == '&':
                ra = self.gen_expr_code(expr.a)
                if not expr.a.lvalue:
                    raise SemanticError('No valid lvalue', expr.a.loc)
                expr.typ = ast.PointerType(expr.a.typ)
                expr.lvalue = False
                return ra
            else:
                raise NotImplementedError('Unknown unop {0}'.format(expr.op))
        elif type(expr) is ast.Identifier:
            # Generate code for this identifier.
            target = self.context.resolve_symbol(expr)
            expr.typ = target.typ

            # This returns the dereferenced variable.
            if isinstance(target, ast.Variable):
                expr.lvalue = True
                return self.varMap[target]
            elif isinstance(target, ast.Constant):
                expr.lvalue = False
                c_val = self.context.get_constant_value(target)
                return self.emit(ir.Const(c_val, target.name, ir.i32))
            else:
                raise NotImplementedError(str(target))
        elif type(expr) is ast.Deref:
            return self.gen_dereference(expr)
        elif type(expr) is ast.Member:
            return self.gen_member_expr(expr)
        elif type(expr) is ast.Index:
            return self.gen_index_expr(expr)
        elif type(expr) is ast.Literal:
            return self.gen_literal_expr(expr)
        elif type(expr) is ast.TypeCast:
            return self.gen_type_cast(expr)
        elif type(expr) is ast.Sizeof:
            # The type of this expression is int:
            expr.lvalue = False  # This is not a location value..
            expr.typ = self.context.intType
            self.context.check_type(expr.query_typ)
            type_size = self.context.size_of(expr.query_typ)
            return self.emit(ir.Const(type_size, 'sizeof', ir.i32))
        elif type(expr) is ast.FunctionCall:
            return self.gen_function_call(expr)
        else:
            raise NotImplementedError('Unknown expr {}'.format(expr))

    def gen_dereference(self, expr):
        """ dereference pointer type: """
        assert type(expr) is ast.Deref
        addr = self.gen_expr_code(expr.ptr)
        ptr_typ = self.context.the_type(expr.ptr.typ)
        expr.lvalue = True
        if type(ptr_typ) is not ast.PointerType:
            raise SemanticError('Cannot deref non-pointer', expr.loc)
        expr.typ = ptr_typ.ptype
        # TODO: why not load the pointed to type?
        load_ty = self.get_ir_type(ptr_typ, expr.loc)
        return self.emit(ir.Load(addr, 'deref', load_ty))

    def gen_binop(self, expr):
        """ Generate code for binary operation """
        assert type(expr) is ast.Binop
        expr.lvalue = False
        a_val = self.make_rvalue_expr(expr.a)
        b_val = self.make_rvalue_expr(expr.b)
        self.analyzer.check_binop(expr)

        # Get best type for result:
        common_type = self.context.get_common_type(expr.a, expr.b)
        expr.typ = common_type

        # TODO: check if operation can be performed on shift and bitwise
        if expr.op not in ['+', '-', '*', '/', '<<', '>>', '|', '&']:
            raise SemanticError("Cannot use {}".format(expr.op))

        # Perform type coercion:
        # TODO: use ir-types, or ast types?
        a_val = self.do_coerce(a_val, expr.a.typ, common_type, expr.loc)
        b_val = self.do_coerce(b_val, expr.b.typ, common_type, expr.loc)

        return self.emit(ir.Binop(a_val, expr.op, b_val, "binop", a_val.ty))

    def gen_member_expr(self, expr):
        """ Generate code for member expression such as struc.mem = 2 """
        base = self.gen_expr_code(expr.base)
        expr.lvalue = expr.base.lvalue
        basetype = self.context.the_type(expr.base.typ)
        if type(basetype) is ast.StructureType:
            if basetype.hasField(expr.field):
                expr.typ = basetype.fieldType(expr.field)
            else:
                raise SemanticError('{} does not contain field {}'
                                    .format(basetype, expr.field),
                                    expr.loc)
        else:
            raise SemanticError('Cannot select {} of non-structure type {}'
                                .format(expr.field, basetype), expr.loc)

        # expr must be lvalue because we handle with addresses of variables
        assert expr.lvalue

        # assert type(base) is ir.Mem, type(base)
        # Calculate offset into struct:
        bt = self.context.the_type(expr.base.typ)
        offset = self.emit(
            ir.Const(bt.fieldOffset(expr.field), 'offset', ir.i32))
        offset = self.emit(ir.IntToPtr(offset, 'offset'))

        # Calculate memory address of field:
        # TODO: Load value when its an l value
        return self.emit(ir.Add(base, offset, "mem_addr", ir.ptr))

    def gen_index_expr(self, expr):
        """ Array indexing """
        base = self.gen_expr_code(expr.base)
        idx = self.make_rvalue_expr(expr.i)

        base_typ = self.context.the_type(expr.base.typ)
        if not isinstance(base_typ, ast.ArrayType):
            raise SemanticError('Cannot index non-array type {}'
                                .format(base_typ),
                                expr.base.loc)

        # Make sure the index is an integer:
        idx = self.do_coerce(idx, expr.i.typ, self.context.intType, expr.i.loc)

        # Base address must be a location value:
        assert expr.base.lvalue
        element_type = self.context.the_type(base_typ.element_type)
        element_size = self.context.size_of(element_type)
        expr.typ = base_typ.element_type
        # print(expr.typ, base_typ)
        expr.lvalue = True

        # Generate constant:
        e_size = self.emit(ir.Const(element_size, 'element_size', ir.i32))

        # Calculate offset:
        offset = self.emit(ir.Mul(idx, e_size, "element_offset", ir.i32))
        offset = self.emit(ir.IntToPtr(offset, 'elem_offset'))

        # Calculate address:
        return self.emit(ir.Add(base, offset, "element_address", ir.ptr))

    def gen_literal_expr(self, expr):
        """ Generate code for literal """
        expr.lvalue = False
        typemap = {int: 'int',
                   float: 'double',
                   bool: 'bool',
                   str: 'string'}
        if type(expr.val) in typemap:
            expr.typ = self.context.scope[typemap[type(expr.val)]]
        else:
            raise SemanticError('Unknown literal type {}'
                                .format(expr.val), expr.loc)
        # Construct correct const value:
        if type(expr.val) is str:
            cval = pack_string(expr.val)
            txt_content = ir.Const(cval, 'strval', ir.i32)
            self.emit(txt_content)
            value = ir.Addr(txt_content, 'addroftxt', ir.i32)
        elif type(expr.val) is int:
            value = ir.Const(expr.val, 'cnst', ir.i32)
        elif type(expr.val) is bool:
            # For booleans, use the integer as storage class:
            v = int(expr.val)
            value = ir.Const(v, 'bool_cnst', ir.i32)
        elif type(expr.val) is float:
            v = float(expr.val)
            value = ir.Const(v, 'bool_cnst', ir.double)
        else:
            raise NotImplementedError()
        return self.emit(value)

    def gen_type_cast(self, expr):
        """ Generate code for type casting """
        # When type casting, the rvalue property is lost.
        ar = self.make_rvalue_expr(expr.a)
        expr.lvalue = False

        from_type = self.context.the_type(expr.a.typ)
        to_type = self.context.the_type(expr.to_type)
        expr.typ = expr.to_type
        if isinstance(from_type, ast.PointerType) and \
                isinstance(to_type, ast.PointerType):
            return ar
        elif self.context.equal_types(self.context.intType, from_type) and \
                isinstance(to_type, ast.PointerType):
            return self.emit(ir.IntToPtr(ar, 'int2ptr'))
        elif self.context.equal_types(self.context.intType, to_type) \
                and isinstance(from_type, ast.PointerType):
            return self.emit(ir.PtrToInt(ar, 'ptr2int'))
        elif type(from_type) is ast.BaseType and from_type.name == 'byte' and \
                type(to_type) is ast.BaseType and to_type.name == 'int':
            return self.emit(ir.ByteToInt(ar, 'byte2int'))
        elif type(from_type) is ast.BaseType and from_type.name == 'int' and \
                type(to_type) is ast.BaseType and to_type.name == 'byte':
            return self.emit(ir.IntToByte(ar, 'bytecast'))
        else:
            raise SemanticError('Cannot cast {} to {}'
                                .format(from_type, to_type), expr.loc)

    def gen_function_call(self, expr):
        """ Generate code for a function call """
        # Evaluate the arguments:
        args = [self.make_rvalue_expr(argument) for argument in expr.args]

        # Check arguments:
        tg = self.context.resolve_symbol(expr.proc)
        if type(tg) is not ast.Function:
            raise SemanticError('cannot call {}'.format(tg))
        ftyp = tg.typ
        fname = tg.package.name + '_' + tg.name
        ptypes = ftyp.parametertypes
        if len(expr.args) != len(ptypes):
            raise SemanticError('{} requires {} arguments, {} given'
                                .format(fname, len(ptypes), len(expr.args)),
                                expr.loc)
        for arg, at in zip(expr.args, ptypes):
            if not self.context.equal_types(arg.typ, at):
                raise SemanticError('Got {}, expected {}'
                                    .format(arg.typ, at), arg.loc)
        # determine return type:
        expr.typ = ftyp.returntype

        expr.lvalue = False
        # TODO: for now, always return i32?
        call = ir.Call(fname, args, fname + '_rv', ir.i32)
        self.emit(call)
        return call
