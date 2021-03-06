import inspect
from abc import ABCMeta
from collections import OrderedDict, MutableSequence
from functools import update_wrapper
from types import MethodType

import six
from six import add_metaclass
from theano import tensor
from theano.gof import Variable

from blocks.graph import add_annotation, Annotation
from blocks.roles import add_role, PARAMETER, INPUT, OUTPUT
from blocks.utils import pack, repr_attrs, reraise_as, unpack


def create_unbound_method(func, cls):
    """Create an unbounded method from a function and a class.

    Notes
    -----
    See https://bitbucket.org/gutworth/six/pull-request/64.

    """
    if six.PY2:
        return MethodType(func, None, cls)
    if six.PY3:
        return func

# Rename built-in property to avoid conflict with Application.property
property_ = property


class Parameters(MutableSequence):
    """Behaves exactly like a list, but annotates the variables."""
    def __init__(self, brick, params):
        self.brick = brick
        self._params = []
        for param in params:
            self.append(param)

    def __repr__(self):
        return repr(self._params)

    def __getitem__(self, key):
        return self._params[key]

    def _annotate(self, value):
        if isinstance(value, Variable):
            add_role(value, PARAMETER)
            add_annotation(value, self.brick)

    def __setitem__(self, key, value):
        self._annotate(value)
        self._params[key] = value

    def __delitem__(self, key):
        del self._params[key]

    def __len__(self):
        return len(self._params)

    def insert(self, index, value):
        self._annotate(value)
        self._params.insert(index, value)


class Application(object):
    """An application method belonging to a particular type of brick.

    The application methods of each :class:`Brick` class are automatically
    replaced by an instance of :class:`Application`. This allows us to
    store metadata about particular application methods (such as their in-
    and outputs) easily.

    Attributes
    ----------
    application : callable
        The original (unbounded) application function defined on the
        :class:`Brick`.
    delegate_function : callable
        A function that takes a :class:`Brick` instance as an argument and
        returns a :class:`BoundApplication` object to which attribute
        requests should be routed.
    properties : :obj:`dict` (:obj:`str`, :obj:`callable`)
        A dictionary of property getters that should be called when an
        attribute with the given name is requested.
    instances : :obj:`dict` (:class:`Brick`, :class:`BoundApplication`)
        A record of bound application instances created by the descriptor
        protocol.
    call_stack : :obj:`list` of :class:`Brick`
        The call stack of brick application methods. Used to check whether
        the current call was made by a parent brick.

    Raises
    ------
    ValueError
        If a brick's application method is applied by another brick which
        does not list the former as a child.
    ValueError
        If the application method's inputs and/or outputs don't match with
        the function signature or the values returned (respectively).

    Notes
    -----
    When a :class:`Brick` is instantiated and its application method (i.e.
    an instance of this class) requested, the descriptor protocol (through
    the :meth:`__get__` method) automatically instantiates a
    :class:`BoundApplication` class and returns this. This bound
    application class can be used to store application information
    particular to a brick instance. Any attributes unknown to the bounded
    application are automatically routed to the application that
    instantiated it.

    """
    call_stack = []

    def __init__(self, application):
        self.application = application
        self.delegate_function = None
        self.properties = {}
        self.bound_applications = {}

    def property(self, name):
        """Decorator to make application properties.

        Parameters
        ----------
        name : str
            The name the property should take.

        Examples
        --------
        >>> class Foo(Brick):
        ...     @application
        ...     def apply(self, x):
        ...         return x + 1
        ...
        ...     @apply.property('inputs')
        ...     def apply_inputs(self):
        ...         return ['foo', 'bar']
        >>> foo = Foo()
        >>> foo.apply.inputs
        ['foo', 'bar']

        """
        if not isinstance(name, six.string_types):
            raise ValueError

        def wrap_property(property_):
            self.properties[name] = property_
            return property_
        return wrap_property

    def delegate(self, f):
        """Decorator to assign a delegate application.

        An application method can assign a delegate application. Whenever
        an attribute is not available, it will be requested from the
        delegate instead.

        Examples
        --------
        >>> class Foo(Brick):
        ...     @application(outputs=['baz'])
        ...     def apply(self, x):
        ...         return x + 1
        ...
        ...     @apply.property('inputs')
        ...     def apply_inputs(self):
        ...         return ['foo', 'bar']
        >>> class Bar(Brick):
        ...     def __init__(self, foo):
        ...         self.foo = foo
        ...
        ...     @application(outputs=['foo'])
        ...     def apply(self, x):
        ...         return x + 1
        ...
        ...     @apply.delegate
        ...     def apply_delegate(self):
        ...         return self.foo.apply
        >>> foo = Foo()
        >>> bar = Bar(foo)
        >>> bar.apply.outputs
        ['foo']
        >>> bar.apply.inputs
        ['foo', 'bar']

        """
        self.delegate_function = f
        return f

    def __get__(self, instance, owner):
        """Instantiate :class:`BoundApplication` for each :class:`Brick`."""
        if instance is None:
            return self
        elif instance not in self.bound_applications:
            bound_application = BoundApplication(self, instance)
            self.bound_applications[instance] = bound_application
        return self.bound_applications[instance]

    def __getattr__(self, name):
        # Mimic behavior of properties
        if 'properties' in self.__dict__ and name in self.properties:
            return property(create_unbound_method(self.properties[name],
                                                  self.brick))
        raise AttributeError

    def __setattr__(self, name, value):
        # Mimic behavior of read-only properties
        if 'properties' in self.__dict__ and name in self.properties:
            raise AttributeError("can't set attribute")
        super(Application, self).__setattr__(name, value)

    @property_
    def inputs(self):
        return self._inputs

    @inputs.setter
    def inputs(self, inputs):
        args_names, varargs_name, _, _ = inspect.getargspec(
            self.application)
        if not all(input_ in args_names + [varargs_name] for input_ in inputs):
            raise ValueError("Unexpected inputs")
        self._inputs = inputs

    @property_
    def name(self):
        return self.application.__name__

    def __call__(self, brick, *args, **kwargs):
        if not isinstance(brick, Brick) and six.PY2:
            raise TypeError
        bound_application = self.__get__(brick, brick.__class__)
        return self.apply(bound_application, *args, **kwargs)

    def apply(self, bound_application, *args, **kwargs):
        return_dict = kwargs.pop('return_dict', False)
        return_list = kwargs.pop('return_list', False)
        if return_list and return_dict:
            raise ValueError

        brick = bound_application.brick

        # Find the names of the inputs to the application method
        args_names, varargs_name, _, _ = inspect.getargspec(
            self.application)
        args_names = args_names[1:]

        # Construct the ApplicationCall, used to store data in for this call
        call = ApplicationCall(brick, bound_application)
        args = list(args)
        if 'application' in args_names:
            args.insert(args_names.index('application'), bound_application)
        if 'application_call' in args_names:
            args.insert(args_names.index('application_call'), call)

        # Allocate before applying, and optionally initialize
        if not brick.allocated:
            brick.allocate()
        if not brick.initialized and not brick.lazy:
            brick.initialize()

        # Annotate all the input variables which are Theano variables
        def copy_and_tag(variable, role, name):
            """Helper method to copy a variable and annotate it."""
            copy = variable.copy()
            # Theano name
            copy.name = _variable_name(brick.name, self.name, name)
            add_annotation(copy, brick)
            add_annotation(copy, call)
            # Blocks name
            copy.tag.name = name
            add_role(copy, role)
            return copy

        for i, input_ in enumerate(args):
            if isinstance(input_, tensor.Variable):
                if i < len(args_names):
                    name = args_names[i]
                else:
                    name = "{}_{}".format(varargs_name, i - len(args_names))
                args[i] = copy_and_tag(input_, INPUT, name)
        for name, input_ in kwargs.items():
            if isinstance(input_, tensor.Variable):
                kwargs[name] = copy_and_tag(input_, INPUT, name)

        # Run the application method on the annotated variables
        if self.call_stack and brick is not self.call_stack[-1] and \
                brick not in self.call_stack[-1].children:
            raise ValueError
        self.call_stack.append(brick)
        try:
            outputs = self.application(brick, *args, **kwargs)
            outputs = pack(outputs)
        finally:
            self.call_stack.pop()

        # Rename and annotate output variables
        for i, output in enumerate(outputs):
            if isinstance(output, tensor.Variable):
                try:
                    name = bound_application.outputs[i]
                except AttributeError:
                    name = "output_{}".format(i)
                except IndexError:
                    reraise_as(ValueError("Unexpected outputs"))
                # TODO Tag with dimensions, axes, etc. for error-checking
                outputs[i] = copy_and_tag(outputs[i],
                                          OUTPUT, name)

        # Return values
        if return_list:
            return outputs
        if return_dict:
            return OrderedDict(zip(bound_application.outputs, outputs))
        return unpack(outputs)


class BoundApplication(object):
    """An application method bound to a :class:`Brick` instance."""
    def __init__(self, application, brick):
        self.application = application
        self.brick = brick

    def __getattr__(self, name):
        # Prevent infinite loops
        if name == 'application':
            raise AttributeError
        # These always belong to the parent (the unbound application)
        if name in ('delegate_function', 'properties'):
            return getattr(self.application, name)
        if name in self.properties:
            return self.properties[name](self.brick)
        # First try the parent (i.e. class level), before trying the delegate
        try:
            return getattr(self.application, name)
        except AttributeError:
            if self.delegate_function:
                return getattr(self.delegate_function(self.brick), name)
            raise

    @property
    def name(self):
        return self.application.name

    def __call__(self, *args, **kwargs):
        return self.application.apply(self, *args, **kwargs)


class _Brick(ABCMeta):
    """Metaclass which attaches brick instances to the applications."""
    def __new__(mcl, name, bases, namespace):
        brick = super(_Brick, mcl).__new__(mcl, name, bases, namespace)
        for attr in namespace.values():
            if isinstance(attr, Application):
                attr.brick = brick
        return brick


@add_metaclass(_Brick)
class Brick(Annotation):
    """A brick encapsulates Theano operations with parameters.

    A brick goes through the following stages:

    1. Construction: The call to :meth:`__init__` constructs a
       :class:`Brick` instance with a name and creates any child bricks as
       well.
    2. Allocation of parameters:

       a) Allocation configuration of children: The
          :meth:`push_allocation_config` method configures any children of
          this block.
       b) Allocation: The :meth:`allocate` method allocates the shared
          Theano variables required for the parameters. Also allocates
          parameters for all children.

    3. The following can be done in either order:

       a) Application: By applying the brick to a set of Theano
          variables a part of the computational graph of the final model is
          constructed.
       b) The initialization of parameters:

          1. Initialization configuration of children: The
             :meth:`push_initialization_config` method configures any
             children of this block.
          2. Initialization: This sets the initial values of the
             parameters by a call to :meth:`initialize`, which is needed
             to call the final compiled Theano function.  Also initializes
             all children.

    Not all stages need to be called explicitly. Step 3(a) will
    automatically allocate the parameters if needed. Similarly, step
    3(b.2) and 2(b) will automatically perform steps 3(b.1) and 2(a) if
    needed. They only need to be called separately if greater control is
    required. The only two methods which always need to be called are an
    application method to construct the computational graph, and the
    :meth:`initialize` method in order to initialize the parameters.

    At each different stage, a brick might need a certain set of
    configuration settings. All of these settings can be passed to the
    :meth:`__init__` constructor. However, by default many bricks support
    *lazy initialization*. This means that the configuration settings can
    be set later.

    .. note::

       Some arguments to :meth:`__init__` are *always* required, even when
       lazy initialization is enabled. Other arguments must be given before
       calling :meth:`allocate`, while others yet only need to be given in
       order to call :meth:`initialize`. Always read the documentation of
       each brick carefully.

    Lazy initialization can be turned off by setting ``Brick.lazy =
    False``. In this case, there is no need to call :meth:`initialize`
    manually anymore, but all the configuration must be passed to the
    :meth:`__init__` method.

    Parameters
    ----------
    name : str, optional
        The name of this brick. This can be used to filter the application
        of certain modifications by brick names. By default, the brick
        receives the name of its class (lowercased).

    Attributes
    ----------
    name : str
        The name of this brick.
    lazy : bool
        ``True`` by default. When bricks are lazy, not all configuration
        needs to be provided to the constructor, allowing it to be set in
        another way after construction. Many parts of the library rely on
        this behavior. However, it does require a separate call to
        :meth:`initialize`. If set to ``False`` on the other hand, bricks
        will be ready to run after construction.
    print_shapes : bool
        ``False`` by default. If ``True`` it logs the shapes of all the
        input and output variables, which can be useful for debugging.
    params : list of :class:`~tensor.TensorSharedVariable` and ``None``
        After calling the :meth:`allocate` method this attribute will be
        populated with the shared variables storing this brick's
        parameters. Allows for ``None`` so that parameters can always be
        accessed at the same index, even if some parameters are only
        defined given a particular configuration.
    children : list of bricks
        The children of this brick.
    allocated : bool
        ``False`` if :meth:`allocate` has not been called yet. ``True``
        otherwise.
    initialized : bool
        ``False`` if :meth:`allocate` has not been called yet. ``True``
        otherwise.
    allocation_config_pushed : bool
        ``False`` if :meth:`allocate` or :meth:`push_allocation_config`
        hasn't been called yet. ``True`` otherwise.
    initialization_config_pushed : bool
        ``False`` if :meth:`initialize` or
        :meth:`push_initialization_config` hasn't been called yet. ``True``
        otherwise.

    Notes
    -----
    To provide support for lazy initialization, apply the :meth:`lazy`
    decorator to the :meth:`__init__` method.

    Brick implementations *must* call the :meth:`__init__` constructor of
    their parent using `super(BlockImplementation,
    self).__init__(**kwargs)` at the *beginning* of the overriding
    `__init__`.

    The methods :meth:`_allocate` and :meth:`_initialize` need to be
    overridden if the brick needs to allocate shared variables and
    initialize their values in order to function.

    A brick can have any number of methods which apply the brick on Theano
    variables. These methods should be decorated with the
    :func:`application` decorator.

    If a brick has children, they must be listed in the :attr:`children`
    attribute. Moreover, if the brick wants to control the configuration of
    its children, the :meth:`_push_allocation_config` and
    :meth:`_push_initialization_config` methods need to be overridden.

    Examples
    --------
    By default, bricks have lazy initialization enabled.

    >>> import theano
    >>> from blocks.initialization import IsotropicGaussian, Constant
    >>> from blocks.bricks import Linear
    >>> linear = Linear(input_dim=5, output_dim=3,
    ...                 weights_init=IsotropicGaussian(),
    ...                 biases_init=Constant(0))
    >>> x = theano.tensor.vector()
    >>> linear.apply(x)  # Calls linear.allocate() automatically
    linear_apply_output
    >>> linear.initialize()  # Initializes the weight matrix

    In simple cases, eager bricks are easier to deal with.

    >>> from blocks.initialization import IsotropicGaussian, Constant
    >>> Brick.lazy = False
    >>> linear = Linear(5, 3, weights_init=IsotropicGaussian(),
    ...                 biases_init=Constant(0))
    >>> linear.apply(x)
    linear_apply_output

    .. doctest::
       :hide:

       >>> Brick.lazy = True  # Reset for other doctests

    """
    #: See :attr:`Brick.lazy`
    lazy = True
    #: See :attr:`Brick.print_shapes`
    print_shapes = False

    def __init__(self, name=None):
        if name is None:
            name = self.__class__.__name__.lower()
        self.name = name

        self.children = []

        self.allocated = False
        self.allocation_config_pushed = False
        self.initialized = False
        self.initialization_config_pushed = False
        super(Brick, self).__init__()

    def __repr__(self):
        return repr_attrs(self, 'name')

    @property
    def params(self):
        return self._params

    @params.setter
    def params(self, value):
        self._params = Parameters(self, value)

    def allocate(self):
        """Allocate shared variables for parameters.

        Based on the current configuration of this :class:`Brick` create
        Theano shared variables to store the parameters.  After allocation,
        parameters are accessible through the :attr:`params` attribute.

        This method calls the :meth:`allocate` method of all children
        first, allowing the :meth:`_allocate` method to override the
        parameters of the children if needed.

        Raises
        ------
        ValueError
            If the configuration of this brick is insufficient to determine
            the number of parameters or their dimensionality to be
            initialized.

        Notes
        -----
        This method sets the :attr:`params` attribute to an empty list.
        This is in order to ensure that calls to this method completely
        reset the parameters.

        """
        if not self.allocation_config_pushed:
            self.push_allocation_config()
        for child in self.children:
            child.allocate()
        self.params = []
        try:
            self._allocate()
        except Exception:
            if self.lazy:
                reraise_as("Lazy initialization is enabled, so please make "
                           "sure you have set all the required configuration "
                           "for this method call.")
            else:
                raise
        self.allocated = True

    def _allocate(self):
        """Brick implementation of parameter initialization.

        Implement this if your brick needs to allocate its parameters.

        .. warning::

           This method should never be called directly. Call
           :meth:`initialize` instead.

        """
        pass

    def initialize(self):
        """Initialize parameters.

        Intialize parameters, such as weight matrices and biases.

        Notes
        -----
        If the brick has not allocated its parameters yet, this method will
        call the :meth:`allocate` method in order to do so.

        """
        if not self.allocated:
            self.allocate()
        if not self.initialization_config_pushed:
            self.push_initialization_config()
        for child in self.children:
            child.initialize()
        try:
            self._initialize()
        except Exception:
            if self.lazy:
                reraise_as("Lazy initialization is enabled, so please make "
                           "sure you have set all the required configuration "
                           "for this method call.")
            else:
                raise
        self.initialized = True

    def _initialize(self):
        """Brick implementation of parameter initialization.

        Implement this if your brick needs to initialize its parameters.

        .. warning::

           This method should never be called directly. Call
           :meth:`initialize` instead.

        """
        pass

    def push_allocation_config(self):
        """Push the configuration for allocation to child bricks.

        Bricks can configure their children, based on their own current
        configuration. This will be automatically done by a call to
        :meth:`allocate`, but if you want to override the configuration of
        child bricks manually, then you can call this function manually.

        """
        self._push_allocation_config()
        self.allocation_config_pushed = True
        for child in self.children:
            try:
                child.push_allocation_config()
            except Exception:
                self.allocation_config_pushed = False
                raise

    def _push_allocation_config(self):
        """Brick implementation of configuring child before allocation.

        Implement this if your brick needs to set the configuration of its
        children before allocation.

        .. warning::

           This method should never be called directly. Call
           :meth:`push_allocation_config` instead.

        """
        pass

    def push_initialization_config(self):
        """Push the configuration for initialization to child bricks.

        Bricks can configure their children, based on their own current
        configuration. This will be automatically done by a call to
        :meth:`initialize`, but if you want to override the configuration
        of child bricks manually, then you can call this function manually.

        """
        self._push_initialization_config()
        self.initialization_config_pushed = True
        for child in self.children:
            try:
                child.push_initialization_config()
            except Exception:
                self.initialization_config_pushed = False
                raise

    def _push_initialization_config(self):
        """Brick implementation of configuring child before initialization.

        Implement this if your brick needs to set the configuration of its
        children before initialization.

        .. warning::

           This method should never be called directly. Call
           :meth:`push_initialization_config` instead.

        """
        pass

    def get_dim(self, name):
        """Get dimension of an input/output variable of a brick.

        Parameters
        ----------
        name : str
            The name of the variable.

        """
        raise ValueError("No dimension information for {} available"
                         .format(name))

    def get_dims(self, names):
        """Get dictionary of dimensions for a set of input/output variables.

        Parameters
        ----------
        names : list of str
            The dictionary of variable names.

        Returns
        -------
        dims : dict
            Dictionary of (variable name, variable dimension) pairs.

        """
        return {name: self.get_dim(name) for name in names}


def lazy(func):
    """Makes the initialization lazy.

    Any positional argument not given will be set to ``None``. Positional
    arguments can also be given as keyword arguments.

    Parameters
    ----------
    func : method
        The __init__ method to make lazy.

    Examples
    --------
    >>> class SomeBrick(Brick):
    ...     @lazy
    ...     def __init__(self, a, b, c='c', d=None):
    ...         print(a, b, c, d)
    >>> brick = SomeBrick('a')
    a None c None
    >>> brick = SomeBrick(d='d', b='b')
    None b c d
    >>> Brick.lazy = False
    >>> brick = SomeBrick('a')
    Traceback (most recent call last):
      ...
    TypeError: __init__() missing 1 required positional argument: 'b'

    .. doctest::
       :hide:

       >>> Brick.lazy = True  # Reset for other doctests

    """
    arg_spec = inspect.getargspec(func)
    arg_names = arg_spec.args[1:]
    defaults = arg_spec.defaults
    if defaults is None:
        defaults = []

    def init(self, *args, **kwargs):
        if not self.lazy:
            return func(self, *args, **kwargs)
        # Fill any missing positional arguments with None
        args = args + (None,) * (len(arg_names) - len(defaults) -
                                 len(args))

        # Check if positional arguments were passed as keyword arguments
        args = list(args)
        for i, arg_name in enumerate(arg_names[:len(arg_names)
                                               - len(defaults)]):
            if arg_name in kwargs:
                if args[i] is not None:
                    raise ValueError
                args[i] = kwargs.pop(arg_name)

        return func(self, *args, **kwargs)
    return init


class ApplicationCall(Annotation):
    """A link between the variable tags and bricks.

    The application call can be used to attach to an apply call auxiliary
    variables (e.g. monitors or regularizers) that do not form part of the
    main computation graph.

    The application call object is created before the call to the
    application method and can be accessed by specifying an
    application_call argument.

    Also see :class:`.Annotation`.

    Parameters
    ----------
    brick : :class:`Brick` instance
        The brick whose application is called
    application : :class:`BoundApplication` instance
        The bound application (i.e. belong to a brick instance) object
        being called

    Examples
    --------
    >>> class Foo(Brick):
    ...     @application
    ...     def apply(self, x, application_call):
    ...         application_call.add_auxiliary_variable(x.mean())
    ...         return x + 1
    >>> x = tensor.vector()
    >>> y = Foo().apply(x)
    >>> from blocks.filter import get_application_call
    >>> get_application_call(y) # doctest: +ELLIPSIS
    <blocks.bricks.base.ApplicationCall object at ...>

    """
    def __init__(self, brick, application):
        self.brick = brick
        self.application = application
        super(ApplicationCall, self).__init__()

    def add_auxiliary_variable(self, variable, roles=None, name=None):
        if name:
            variable.name = _variable_name(
                self.brick.name, self.application.name, name)
            variable.tag.name = name
            name = None
        return super(ApplicationCall, self).add_auxiliary_variable(
            variable, roles, name)


def application(*args, **kwargs):
    r"""Decorator for methods that apply a brick to inputs.

    Parameters
    ----------
    \*args, optional
        The application method to wrap.
    \*\*kwargs, optional
        Attributes to attach to this application.

    Notes
    -----
    This decorator replaces application methods with :class:`Application`
    instances. It also sets the attributes given as keyword arguments to
    the decorator.

    Examples
    --------
    >>> class Foo(Brick):
    ...     @application(inputs=['x'], outputs=['y'])
    ...     def apply(self, x):
    ...         return x + 1
    ...     @application
    ...     def other_apply(self, x):
    ...         return x - 1
    >>> foo = Foo()
    >>> Foo.apply.inputs
    ['x']
    >>> foo.apply.outputs
    ['y']
    >>> Foo.other_apply # doctest: +ELLIPSIS
    <blocks.bricks.base.Application object at ...>

    """
    if not ((args and not kwargs) or (not args and kwargs)):
        raise ValueError
    if args:
        application_function, = args
        application = Application(application_function)
        update_wrapper(application, application_function)
        return application
    else:
        def wrap_application(application_function):
            application = Application(application_function)
            update_wrapper(application, application_function)
            for key, value in kwargs.items():
                setattr(application, key, value)
            return application
        return wrap_application


def _variable_name(brick_name, application_name, name):
    return "{}_{}_{}".format(brick_name, application_name, name)
