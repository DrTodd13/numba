#from ._ndarray_utils import _transmogrify
import numpy as np
from inspect import getmembers, isfunction, isclass, isbuiltin
from numbers import Number
import numba
from types import FunctionType as ftype, BuiltinFunctionType as bftype
from numba import types
from numba.extending import typeof_impl, register_model, type_callable, lower_builtin
from numba.np import numpy_support
from numba.core.pythonapi import box, allocator
from llvmlite import ir
import llvmlite.llvmpy.core as lc
import llvmlite.binding as llb
from numba.core import types, cgutils
import builtins
import sys
from ctypes.util import find_library
from numba.core.typing.templates import builtin_registry as templates_registry
from numba.core.typing.npydecl import registry as typing_registry
from numba.core.imputils import builtin_registry as lower_registry
import importlib
import functools
import inspect
from numba.core.typing.templates import CallableTemplate
from numba.np.arrayobj import _array_copy

debug = False

def dprint(*args):
    if debug:
        print(*args)
        sys.stdout.flush()

flib = find_library('mkl_intel_ilp64')
dprint("flib:", flib)
llb.load_library_permanently(flib)

sycl_mem_lib = find_library('DPPLSyclInterface')
dprint("sycl_mem_lib:", sycl_mem_lib)
llb.load_library_permanently(sycl_mem_lib)

import dpctl
from dpctl._memory import MemoryUSMShared
import numba.dppl._dppl_rt

functions_list = [o[0] for o in getmembers(np) if isfunction(o[1]) or isbuiltin(o[1])]
class_list = [o for o in getmembers(np) if isclass(o[1])]
# Register the helper function in dppl_rt so that we can insert calls to them via llvmlite.
for py_name, c_address in numba.dppl._dppl_rt.c_helpers.items():
    llb.add_symbol(py_name, c_address)

array_interface_property = "__array_interface__"
def has_array_interface(x):
    return hasattr(x, array_interface_property)

class ndarray(np.ndarray):
    """
    numpy.ndarray subclass whose underlying memory buffer is allocated
    with a foreign allocator.
    """
    def __new__(subtype, shape,
                dtype=float, buffer=None, offset=0,
                strides=None, order=None):
        # Create a new array.
        if buffer is None:
            dprint("dparray::ndarray __new__ buffer None")
            nelems = np.prod(shape)
            dt = np.dtype(dtype)
            isz = dt.itemsize
            buf = MemoryUSMShared(nbytes=isz*max(1,nelems))
            new_obj = np.ndarray.__new__(
                subtype, shape, dtype=dt,
                buffer=buf, offset=0,
                strides=strides, order=order)
            if hasattr(new_obj, array_interface_property):
                dprint("buffer None new_obj already has sycl_usm")
            else:
                dprint("buffer None new_obj will add sycl_usm")
                new_obj.__sycl_usm_array_interface__ = {}
            return new_obj
        # zero copy if buffer is a usm backed array-like thing
        elif hasattr(buffer, array_interface_property):
            dprint("dparray::ndarray __new__ buffer", array_interface_property)
            # also check for array interface
            new_obj = np.ndarray.__new__(
                subtype, shape, dtype=dtype,
                buffer=buffer, offset=offset,
                strides=strides, order=order)
            if hasattr(new_obj, array_interface_property):
                dprint("buffer None new_obj already has sycl_usm")
            else:
                dprint("buffer None new_obj will add sycl_usm")
                new_obj.__sycl_usm_array_interface__ = {}
            return new_obj
        else:
            dprint("dparray::ndarray __new__ buffer not None and not sycl_usm")
            nelems = np.prod(shape)
            # must copy
            ar = np.ndarray(shape,
                            dtype=dtype, buffer=buffer,
                            offset=offset, strides=strides,
                            order=order)
            buf = MemoryUSMShared(nbytes=ar.nbytes)
            new_obj = np.ndarray.__new__(
                subtype, shape, dtype=dtype,
                buffer=buf, offset=0,
                strides=strides, order=order)
            np.copyto(new_obj, ar, casting='no')
            if hasattr(new_obj, array_interface_property):
                dprint("buffer None new_obj already has sycl_usm")
            else:
                dprint("buffer None new_obj will add sycl_usm")
                new_obj.__sycl_usm_array_interface__ = {}
            return new_obj

    def __array_finalize__(self, obj):
        dprint("__array_finalize__:", obj, hex(id(obj)), type(obj))
#        import pdb
#        pdb.set_trace()
        # When called from the explicit constructor, obj is None
        if obj is None: return
        # When called in new-from-template, `obj` is another instance of our own
        # subclass, that we might use to update the new `self` instance.
        # However, when called from view casting, `obj` can be an instance of any
        # subclass of ndarray, including our own.
        if hasattr(obj, array_interface_property):
            return
        if isinstance(obj, numba.core.runtime._nrt_python._MemInfo):
            mobj = obj
            while isinstance(mobj, numba.core.runtime._nrt_python._MemInfo):
                dprint("array_finalize got Numba MemInfo")
                ea = mobj.external_allocator
                d = mobj.data
                dprint("external_allocator:", hex(ea), type(ea))
                dprint("data:", hex(d), type(d))
                dppl_rt_allocator = numba.dppl._dppl_rt.get_external_allocator()
                dprint("dppl external_allocator:", hex(dppl_rt_allocator), type(dppl_rt_allocator))
                dprint(dir(mobj))
                if ea == dppl_rt_allocator:
                    return
                mobj = mobj.parent
                if isinstance(mobj, ndarray):
                    mobj = mobj.base
        if isinstance(obj, np.ndarray):
            ob = self
            while isinstance(ob, np.ndarray):
                if hasattr(obj, array_interface_property):
                    return
                ob = ob.base
    
        # Just raise an exception since __array_ufunc__ makes all reasonable cases not
        # need the code below.
        raise ValueError("Non-MKL allocated ndarray can not viewed as MKL-allocated one without a copy")
      
        """
        # since dparray must have mkl_memory underlying it, a copy must be made
        newbuf = dpctl.Memory(nbytes=self.data.nbytes)
        new_arr = np.ndarray.__new__(
            type(self),
            self.shape,
            buffer=newbuf, offset=0,
            dtype=self.dtype,
            strides=self.strides)
        np.copyto(new_arr, self)
        # We need to modify self to now be mkl_memory-backed ndarray
        # We only need to change data and base, but these are not writeable.
        #
        # Modification can not be done by simply setting self either,
        # as self is just a local copy of the instance.
        #
        # raise ValueError("Non-MKL allocated ndarray can not viewed as MKL-allocated one without a copy")
        # Will probably have to raise an exception soon as Numpy may disallow this.
        _transmogrify(self, new_arr)
        """

    # Tell Numba to not treat this type just like a NumPy ndarray but to propagate its type.
    # This way it will use the custom dparray allocator.
    __numba_no_subtype_ndarray__ = True

    # Convert to a NumPy ndarray.
    def as_ndarray(self):
         return np.copy(self)

    def __array__(self):
        return self

    def __array_ufunc__(self, ufunc, method, *inputs, **kwargs):
        if method == '__call__':
            N = None
            scalars = []
            typing = []
            for inp in inputs:
                if isinstance(inp, Number):
                    scalars.append(inp)
                    typing.append(inp)
                elif isinstance(inp, (self.__class__, np.ndarray)):
                    if isinstance(inp, self.__class__):
                        scalars.append(np.ndarray(inp.shape, inp.dtype, inp))
                        typing.append(np.ndarray(inp.shape, inp.dtype))
                    else:
                        scalars.append(inp)
                        typing.append(inp)
                    if N is not None:
                        if N != inp.shape:
                            raise TypeError("inconsistent sizes")
                    else:
                        N = inp.shape
                else:
                    return NotImplemented
            # Have to avoid recursive calls to array_ufunc here.
            # If no out kwarg then we create a dparray out so that we get
            # USM memory.  However, if kwarg has dparray-typed out then
            # array_ufunc is called recursively so we cast out as regular
            # NumPy ndarray (having a USM data pointer).
            if kwargs.get('out', None) is None:
                # maybe copy?
                # deal with multiple returned arrays, so kwargs['out'] can be tuple
                res_type = np.result_type(*typing)
                out = empty(inputs[0].shape, dtype=res_type)
                out_as_np = np.ndarray(out.shape, out.dtype, out)
                kwargs['out'] = out_as_np
            else:
                # If they manually gave dparray as out kwarg then we have to also
                # cast as regular NumPy ndarray to avoid recursion.
                if isinstance(kwargs['out'], ndarray):
                    out = kwargs['out']
                    kwargs['out'] = np.ndarray(out.shape, out.dtype, out)
                else:
                    out = kwargs['out']
            ret = ufunc(*scalars, **kwargs)
            return out
        else:
            return NotImplemented

for c in class_list:
    cname = c[0]
    # For now we do the simple thing and copy the types from NumPy module into dparray module.
    new_func = "%s = np.%s" % (cname, cname)
#    new_func =  "class %s(np.%s):\n" % (cname, cname)
    if cname == "ndarray":
        # Implemented explicitly above.
        continue
    else:
        # This is temporary.
#        new_func += "    pass\n"
        # The code below should eventually be made to work and used.
#        new_func += "    @classmethod\n"
#        new_func += "    def cast(cls, some_np_obj):\n"
#        new_func += "        some_np_obj.__class__ = cls\n"
#        new_func += "        return some_np_obj\n"
        pass
    try:
        the_code = compile(new_func, '__init__', 'exec')
        exec(the_code)
    except:
        print("Failed to exec class", cname)
        pass

# Redefine all Numpy functions in this module and if they
# return a Numpy array, transform that to a USM-backed array
# instead.  This is a stop-gap.  We should eventually find a
# way to do the allocation correct to start with.
for fname in functions_list:
#    print("Adding function", fname)
    new_func =  "def %s(*args, **kwargs):\n" % fname
    new_func += "    ret = np.%s(*args, **kwargs)\n" % fname
    new_func += "    if type(ret) == np.ndarray:\n"
    new_func += "        ret = ndarray(ret.shape, ret.dtype, ret)\n"
    new_func += "    return ret\n"
    the_code = compile(new_func, '__init__', 'exec')
    exec(the_code)

# This class creates a type in Numba.
class DPArrayType(types.Array):
    def __init__(self, dtype, ndim, layout, readonly=False, name=None,
                 aligned=True, addrspace=None):
        # This name defines how this type will be shown in Numba's type dumps.
        name = "DPArray:ndarray(%s, %sd, %s)" % (dtype, ndim, layout)
        super(DPArrayType, self).__init__(dtype, ndim, layout,
                                            py_type=ndarray,
                                            readonly=readonly,
                                            name=name,
                                            addrspace=addrspace)

    # Tell Numba typing how to combine DPArrayType with other ndarray types.
    def __array_ufunc__(self, ufunc, method, *inputs, **kwargs):
        if method == '__call__':
            for inp in inputs:
                if not isinstance(inp, (DPArrayType, types.Array, types.Number)):
                    return None

            return DPArrayType
        else:
            return None

# This tells Numba how to create a DPArrayType when a dparray is passed
# into a njit function.
@typeof_impl.register(ndarray)
def typeof_ta_ndarray(val, c):
    try:
        dtype = numpy_support.from_dtype(val.dtype)
    except NotImplementedError:
        raise ValueError("Unsupported array dtype: %s" % (val.dtype,))
    layout = numpy_support.map_layout(val)
    readonly = not val.flags.writeable
    return DPArrayType(dtype, val.ndim, layout, readonly=readonly)

# This tells Numba to use the default Numpy ndarray data layout for
# object of type DPArray.
register_model(DPArrayType)(numba.core.datamodel.models.ArrayModel)

"""
# This code should not work because you can't pass arbitrary buffer to dparray constructor.

# This tells Numba how to type calls to a DPArray constructor.
@type_callable(ndarray)
def type_ndarray(context):
    def typer(shape, ndim, buf):
        return DPArrayType(buf.dtype, buf.ndim, buf.layout)
    return typer

@overload(ndarray)
def overload_ndarray_constructor(shape, dtype, buf):
    print("overload_ndarray_constructor:", shape, dtype, buf)

    def ndarray_impl(shape, dtype, buf):
        pass

    return ndarray_impl

# This tells Numba how to implement calls to a DPArray constructor.
@lower_builtin(ndarray, types.UniTuple, types.DType, types.Array)
def impl_ndarray(context, builder, sig, args):
    # Need to allocate and copy here!
    shape, ndim, buf = args
    return buf

    context.nrt._require_nrt()

    mod = builder.module
    u32 = ir.IntType(32)

    # Get the Numba external allocator for USM memory.
    ext_allocator_fnty = ir.FunctionType(cgutils.voidptr_t, [])
    ext_allocator_fn = mod.get_or_insert_function(ext_allocator_fnty,
                                    name="dparray_get_ext_allocator")
    ext_allocator = builder.call(ext_allocator_fn, [])
    # Get the Numba function to allocate an aligned array with an external allocator.
    fnty = ir.FunctionType(cgutils.voidptr_t, [cgutils.intp_t, u32, cgutils.voidptr_t])
    fn = mod.get_or_insert_function(fnty,
                                    name="NRT_MemInfo_alloc_safe_aligned_external")
    fn.return_value.add_attribute("noalias")
    if isinstance(align, builtins.int):
        align = context.get_constant(types.uint32, align)
    else:
        assert align.type == u32, "align must be a uint32"
    newary = builder.call(fn, [size, align, ext_allocator])

    return buf
"""

# This tells Numba how to convert from its native representation
# of a DPArray in a njit function back to a Python DPArray.
@box(DPArrayType)
def box_array(typ, val, c):
    nativearycls = c.context.make_array(typ)
    nativeary = nativearycls(c.context, c.builder, value=val)
    if c.context.enable_nrt:
        np_dtype = numpy_support.as_dtype(typ.dtype)
        dtypeptr = c.env_manager.read_const(c.env_manager.add_const(np_dtype))
        # Steals NRT ref
        newary = c.pyapi.nrt_adapt_ndarray_to_python(typ, val, dtypeptr)
        return newary
    else:
        parent = nativeary.parent
        c.pyapi.incref(parent)
        return parent

# This tells Numba to use this function when it needs to allocate a
# DPArray in a njit function.
@allocator(DPArrayType)
def allocator_DPArray(context, builder, size, align):
    context.nrt._require_nrt()

    mod = builder.module
    u32 = ir.IntType(32)

    # Get the Numba external allocator for USM memory.
    ext_allocator_fnty = ir.FunctionType(cgutils.voidptr_t, [])
    ext_allocator_fn = mod.get_or_insert_function(ext_allocator_fnty,
                                    name="dparray_get_ext_allocator")
    ext_allocator = builder.call(ext_allocator_fn, [])
    # Get the Numba function to allocate an aligned array with an external allocator.
    fnty = ir.FunctionType(cgutils.voidptr_t, [cgutils.intp_t, u32, cgutils.voidptr_t])
    fn = mod.get_or_insert_function(fnty,
                                    name="NRT_MemInfo_alloc_safe_aligned_external")
    fn.return_value.add_attribute("noalias")
    if isinstance(align, builtins.int):
        align = context.get_constant(types.uint32, align)
    else:
        assert align.type == u32, "align must be a uint32"
    return builder.call(fn, [size, align, ext_allocator])

registered = False

def numba_register():
    global registered
    if not registered:
        registered = True
        numba_register_typing()
        numba_register_lower_builtin()

# Copy a function registered as a lowerer in Numba but change the
# "np" import in Numba to point to dparray instead of NumPy.
def copy_func_for_dparray(f, dparray_mod):
    import copy as cc
    # Make a copy so our change below doesn't affect anything else.
    gglobals = cc.copy(f.__globals__)
    # Make the "np"'s in the code use dparray instead of Numba's default NumPy.
    gglobals['np'] = dparray_mod
    # Create a new function using the original code but the new globals.
    g = ftype(f.__code__, gglobals, None, f.__defaults__, f.__closure__)
    # Some other tricks to make sure the function copy works.
    g = functools.update_wrapper(g, f)
    g.__kwdefaults__ = f.__kwdefaults__
    return g

def types_replace_array(x):
    return tuple([z if z != types.Array else DPArrayType for z in x])

def numba_register_lower_builtin():
    todo = []
    todo_builtin = []
    todo_getattr = []

    # For all Numpy identifiers that have been registered for typing in Numba...
    # this registry contains functions, getattrs, setattrs, casts and constants...need to do them all? FIX FIX FIX
    for ig in lower_registry.functions:
#        print("ig:", ig, type(ig), len(ig))
        impl, func, types = ig
#        print("register lower_builtin:", impl, type(impl), func, type(func), types, type(types))
        # If it is a Numpy function...
        if isinstance(func, ftype):
#            print("isfunction:", func.__module__, type(func.__module__))
            if func.__module__ == np.__name__:
#                print("name:", func.__name__)
                # If we have overloaded that function in the dparray module (always True right now)...
                if func.__name__ in functions_list:
                    todo.append(ig)
        if isinstance(func, bftype):
#            print("isbuiltinfunction:", func.__module__, type(func.__module__))
            if func.__module__ == np.__name__:
#                print("name:", func.__name__)
                # If we have overloaded that function in the dparray module (always True right now)...
                if func.__name__ in functions_list:
                    todo.append(ig)
#                    print("todo_builtin added:", func.__name__)

    for lg in lower_registry.getattrs:
        func, attr, types = lg
        types_with_dparray = types_replace_array(types)
        if DPArrayType in types_with_dparray:
            dprint("lower_getattr:", func, type(func), attr, type(attr), types, type(types))
            todo_getattr.append((func, attr, types_with_dparray))

    for lg in todo_getattr:
        lower_registry.getattrs.append(lg)

    cur_mod = importlib.import_module(__name__)
    for impl, func, types in (todo+todo_builtin):
        dparray_func = eval(func.__name__)
        dprint("need to re-register lowerer for dparray", impl, func, types, dparray_func)
        new_impl = copy_func_for_dparray(impl, cur_mod)
#        lower_registry.functions.append((impl, dparray_func, types))
        lower_registry.functions.append((new_impl, dparray_func, types))

def argspec_to_string(argspec):
    first_default_arg = len(argspec.args)-len(argspec.defaults)
    non_def = argspec.args[:first_default_arg]
    arg_zip = list(zip(argspec.args[first_default_arg:], argspec.defaults))
    combined = [a+"="+str(b) for a,b in arg_zip]
    return ",".join(non_def + combined)

def numba_register_typing():
    todo = []
    todo_classes = []
    todo_getattr = []

    # For all Numpy identifiers that have been registered for typing in Numba...
    for ig in typing_registry.globals:
        val, typ = ig
#        print("global typing:", val, type(val), typ, type(typ))
        # If it is a Numpy function...
        if isinstance(val, (ftype, bftype)):
#            print("name:", val.__name__, val.__name__ in functions_list)
            # If we have overloaded that function in the dparray module (always True right now)...
            if val.__name__ in functions_list:
                todo.append(ig)
        if isinstance(val, type):
            todo_classes.append(ig)

    for tgetattr in templates_registry.attributes:
        if tgetattr.key == types.Array:
            todo_getattr.append(tgetattr)

    # This is actuallya no-op now.
#    for val, typ in todo_classes:
#        print("todo_classes:", val, type(val), typ, type(typ))
#        assert len(typ.templates) == 1
#        dpval = eval(val.__name__)

    for val, typ in todo:
        assert len(typ.templates) == 1
        # template is the typing class to invoke generic() upon.
        template = typ.templates[0]
        dpval = eval(val.__name__)
        dprint("need to re-register for dparray", val, typ, typ.typing_key)
        """
        if debug:
            print("--------------------------------------------------------------")
            print("need to re-register for dparray", val, typ, typ.typing_key)
            print("val:", val, type(val), "dir val", dir(val))
            print("typ:", typ, type(typ), "dir typ", dir(typ))
            print("typing key:", typ.typing_key)
            print("name:", typ.name)
            print("key:", typ.key)
            print("templates:", typ.templates)
            print("template:", template, type(template))
            print("dpval:", dpval, type(dpval))
            print("--------------------------------------------------------------")
        """

        class_name = "DparrayTemplate_" + val.__name__

        @classmethod
        def set_key_original(cls, key, original):
            cls.key = key
            cls.original = original

        def generic_impl(self):
#            print("generic_impl", self.__class__.key, self.__class__.original)
            original_typer = self.__class__.original.generic(self.__class__.original)
            #print("original_typer:", original_typer, type(original_typer), self.__class__)
            ot_argspec = inspect.getfullargspec(original_typer)
            #print("ot_argspec:", ot_argspec)
            astr = argspec_to_string(ot_argspec)
            #print("astr:", astr)

            typer_func = """def typer({}):
                                original_res = original_typer({})
                                #print("original_res:", original_res)
                                if isinstance(original_res, types.Array):
                                    return DPArrayType(dtype=original_res.dtype, ndim=original_res.ndim, layout=original_res.layout)

                                return original_res""".format(astr, ",".join(ot_argspec.args))

            #print("typer_func:", typer_func)

            try:
                gs = globals()
                ls = locals()
                gs["original_typer"] = ls["original_typer"]
                exec(typer_func, globals(), locals())
            except NameError as ne:
                print("NameError in exec:", ne)
                sys.exit(0)
            except:
                print("exec failed!", sys.exc_info()[0])
                sys.exit(0)

            try:
                exec_res = eval("typer")
            except NameError as ne:
                print("NameError in eval:", ne)
                sys.exit(0)
            except:
                print("eval failed!", sys.exc_info()[0])
                sys.exit(0)

            #print("exec_res:", exec_res)
            return exec_res

        new_dparray_template = type(class_name, (template,), {
            "set_class_vars" : set_key_original,
            "generic" : generic_impl})

        new_dparray_template.set_class_vars(dpval, template)

        assert(callable(dpval))
        type_handler = types.Function(new_dparray_template)
        typing_registry.register_global(dpval, type_handler)

    # Handle dparray attribute typing.
    for tgetattr in todo_getattr:
        class_name = tgetattr.__name__ + "_dparray"
        dprint("tgetattr:", tgetattr, type(tgetattr), class_name)

        @classmethod
        def set_key(cls, key):
            cls.key = key

        def getattr_impl(self, attr):
            if attr.startswith('resolve_'):
                #print("getattr_impl starts with resolve_:", self, type(self), attr)
                def wrapper(*args, **kwargs):
                    attr_res = tgetattr.__getattribute__(self, attr)(*args, **kwargs)
                    if isinstance(attr_res, types.Array):
                        return DPArrayType(dtype=attr_res.dtype, ndim=attr_res.ndim, layout=attr_res.layout)
                return wrapper
            else:
                return tgetattr.__getattribute__(self, attr)

        new_dparray_template = type(class_name, (tgetattr,), {
            "set_class_vars" : set_key,
            "__getattribute__" : getattr_impl})

        new_dparray_template.set_class_vars(DPArrayType)
        templates_registry.register_attr(new_dparray_template)


def from_ndarray(x):
    return copy(x)

def as_ndarray(x):
     return np.copy(x)

@typing_registry.register_global(as_ndarray)
class DparrayAsNdarray(CallableTemplate):
    def generic(self):
        def typer(arg):
            return types.Array(dtype=arg.dtype, ndim=arg.ndim, layout=arg.layout)

        return typer

@typing_registry.register_global(from_ndarray)
class DparrayFromNdarray(CallableTemplate):
    def generic(self):
        def typer(arg):
            return DPArrayType(dtype=arg.dtype, ndim=arg.ndim, layout=arg.layout)

        return typer

@lower_registry.lower(as_ndarray, DPArrayType)
def dparray_conversion_as(context, builder, sig, args):
    return _array_copy(context, builder, sig, args)

@lower_registry.lower(from_ndarray, types.Array)
def dparray_conversion_from(context, builder, sig, args):
    return _array_copy(context, builder, sig, args)
