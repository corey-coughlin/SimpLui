# -*- coding: utf-8 -*-
#
# Copyright 2012-2015 Spotify AB
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
"""
The abstract :py:class:`Task` class.
It is a central concept of simplui and represents the state of the workflow.
See :doc:`/tasks` for an overview.
"""
from collections import deque, OrderedDict
from contextlib import contextmanager
import logging
import traceback
import warnings
import json
import hashlib
import re
import copy
import functools
from typing import Any, Dict, Optional

import simplui

from simplui import configuration
from simplui import parameter
from simplui.task_register import Register
from simplui.parameter import ParameterVisibility
from simplui.parameter import UnconsumedParameterWarning

Parameter = parameter.Parameter
logger = logging.getLogger('simplui-interface')


TASK_ID_INCLUDE_PARAMS = 3
TASK_ID_TRUNCATE_PARAMS = 16
TASK_ID_TRUNCATE_HASH = 10
TASK_ID_INVALID_CHAR_REGEX = re.compile(r'[^A-Za-z0-9_]')
_SAME_AS_PYTHON_MODULE = '_same_as_python_module'

class Flow:
    """
    A Flow is a collection of tasks that are run together.

    This is a simple container for tasks, which can be used to group tasks
    together. Will add extra functionality to this in the future, but simple for now
    """
    def __init__(self, iname=''):
        self.taskset = {}
        self.name = iname
        self.autoRename = True
    def getUniqueName(self, ctask):
        """
        Returns a unique name for the task, based on the name and the number of identical tasks.
        """
        incval = 0
        newname = ctask.name
        while newname in self.taskset and incval < 100000:
            newname = f"{self.name}_{incval}"
            incval += 1
        if newname not in self.taskset:
            return newname
        else:
            raise ValueError(f"Could not find a unique name for task {ctask.name} in flow {self.name}")
    def add(self, intask):
        """
        Add a task to the flow.

        :param task: A simplui.Task instance.
        """
        if not isinstance(intask, Task):
            raise TypeError("Only simplui.Task instances can be added to a Flow")
        if self.autoRename:
            intask.name = self.getUniqueName(intask)
        elif intask.name in self.taskset:
            raise ValueError("Task with name '{}' already exists in the flow".format(intask.name))
        self.taskset[intask.name] = intask

    def __iter__(self):
        return iter(self.tasks)



def task_id_str(task_family, params):
    """
    Returns a canonical string used to identify a particular task

    :param task_family: The task family (class name) of the task
    :param params: a dict mapping parameter names to their serialized values
    :return: A unique, shortened identifier corresponding to the family and params
    """
    # task_id is a concatenation of task family, the first values of the first 3 parameters
    # sorted by parameter name and a md5hash of the family/parameters as a cananocalised json.
    param_str = json.dumps(params, separators=(',', ':'), sort_keys=True)
    param_hash = hashlib.new('md5', param_str.encode('utf-8'), usedforsecurity=False).hexdigest()

    param_summary = '_'.join(p[:TASK_ID_TRUNCATE_PARAMS]
                             for p in (params[p] for p in sorted(params)[:TASK_ID_INCLUDE_PARAMS]))
    param_summary = TASK_ID_INVALID_CHAR_REGEX.sub('_', param_summary)

    return '{}_{}_{}'.format(task_family, param_summary, param_hash[:TASK_ID_TRUNCATE_HASH])


class BulkCompleteNotImplementedError(NotImplementedError):
    """This is here to trick pylint.

    pylint thinks anything raising NotImplementedError needs to be implemented
    in any subclass. bulk_complete isn't like that. This tricks pylint into
    thinking that the default implementation is a valid implementation and not
    an abstract method."""
    pass


class Task(metaclass=Register):
    """
    This is the base class of all simplui Tasks, the base unit of work in simplui.

    A simplui Task describes a unit or work.

    The key methods of a Task, which must be implemented in a subclass are:

    * :py:meth:`run` - the computation done by this task.
    * :py:meth:`requires` - the list of Tasks that this Task depends on.
    * :py:meth:`output` - the output :py:class:`Target` that this Task creates.

    Each :py:class:`~simplui.Parameter` of the Task should be declared as members:

    .. code:: python

        class MyTask(simplui.Task):
            count = simplui.IntParameter()
            second_param = simplui.Parameter()

    In addition to any declared properties and methods, there are a few
    non-declared properties, which are created by the :py:class:`Register`
    metaclass:

    """

    _event_callbacks: Dict[Any, Any] = {}

    #: Priority of the task: the scheduler should favor available
    #: tasks with higher priority values first.
    #: See :ref:`Task.priority`
    priority = 0
    disabled = False

    #: Resources used by the task. Should be formatted like {"scp": 1} to indicate that the
    #: task requires 1 unit of the scp resource.
    resources: Dict[str, Any] = {}

    #: Number of seconds after which to time out the run function.
    #: No timeout if set to 0.
    #: Defaults to 0 or worker-timeout value in config
    worker_timeout: Optional[int] = None

    #: Maximum number of tasks to run together as a batch. Infinite by default
    max_batch_size = float('inf')

    @property
    def batchable(self):
        """
        True if this instance can be run as part of a batch. By default, True
        if it has any batched parameters
        """
        return bool(self.batch_param_names())

    @property
    def retry_count(self):
        """
        Override this positive integer to have different ``retry_count`` at task level
        Check :ref:`scheduler-config`
        """
        return None

    @property
    def disable_hard_timeout(self):
        """
        Override this positive integer to have different ``disable_hard_timeout`` at task level.
        Check :ref:`scheduler-config`
        """
        return None

    @property
    def disable_window(self):
        """
        Override this positive integer to have different ``disable_window`` at task level.
        Check :ref:`scheduler-config`
        """
        return None

    @property
    def disable_window_seconds(self):
        warnings.warn("Use of `disable_window_seconds` has been deprecated, use `disable_window` instead", DeprecationWarning)
        return self.disable_window

    @property
    def owner_email(self):
        '''
        Override this to send out additional error emails to task owner, in addition to the one
        defined in the global configuration. This should return a string or a list of strings. e.g.
        'test@exmaple.com' or ['test1@example.com', 'test2@example.com']
        '''
        return None

    def _owner_list(self):
        """
        Turns the owner_email property into a list. This should not be overridden.
        """
        owner_email = self.owner_email
        if owner_email is None:
            return []
        elif isinstance(owner_email, str):
            return owner_email.split(',')
        else:
            return owner_email

    @property
    def use_cmdline_section(self):
        ''' Property used by core config such as `--workers` etc.
        These will be exposed without the class as prefix.'''
        return True

    @classmethod
    def event_handler(cls, event):
        """
        Decorator for adding event handlers.
        """
        def wrapped(callback):
            cls._event_callbacks.setdefault(cls, {}).setdefault(event, set()).add(callback)
            return callback
        return wrapped

    @classmethod
    def remove_event_handler(cls, event, callback):
        """
        Function to remove the event handler registered previously by the cls.event_handler decorator.
        """
        cls._event_callbacks[cls][event].remove(callback)

    def trigger_event(self, event, *args, **kwargs):
        """
        Trigger that calls all of the specified events associated with this class.
        """
        for event_class, event_callbacks in self._event_callbacks.items():
            if not isinstance(self, event_class):
                continue
            for callback in event_callbacks.get(event, []):
                try:
                    # callbacks are protected
                    callback(*args, **kwargs)
                except KeyboardInterrupt:
                    return
                except BaseException:
                    logger.exception("Error in event callback for %r", event)

    @property
    def accepts_messages(self):
        """
        For configuring which scheduler messages can be received. When falsy, this tasks does not
        accept any message. When True, all messages are accepted.
        """
        return False

    @property
    def task_module(self):
        ''' Returns what Python module to import to get access to this class. '''
        # TODO(erikbern): we should think about a language-agnostic mechanism
        return self.__class__.__module__

    _visible_in_registry = True  # TODO: Consider using in simplui.util as well

    __not_user_specified = '__not_user_specified'

    # This is here just to help pylint, the Register metaclass will always set
    # this value anyway.
    _namespace_at_class_time = None

    task_namespace = __not_user_specified
    """
    This value can be overridden to set the namespace that will be used.
    (See :ref:`Task.namespaces_famlies_and_ids`)
    If it's not specified and you try to read this value anyway, it will return
    garbage. Please use :py:meth:`get_task_namespace` to read the namespace.

    Note that setting this value with ``@property`` will not work, because this
    is a class level value.
    """

    @classmethod
    def get_task_namespace(cls):
        """
        The task family for the given class.

        Note: You normally don't want to override this.
        """
        if cls.task_namespace != cls.__not_user_specified:
            return cls.task_namespace
        elif cls._namespace_at_class_time == _SAME_AS_PYTHON_MODULE:
            return cls.__module__
        return cls._namespace_at_class_time

    @property
    def task_family(self):
        """
        DEPRECATED since after 2.4.0. See :py:meth:`get_task_family` instead.
        Hopefully there will be less meta magic in simplui.

        Convenience method since a property on the metaclass isn't directly
        accessible through the class instances.
        """
        return self.__class__.task_family

    @classmethod
    def get_task_family(cls):
        """
        The task family for the given class.

        If ``task_namespace`` is not set, then it's simply the name of the
        class.  Otherwise, ``<task_namespace>.`` is prefixed to the class name.

        Note: You normally don't want to override this.
        """
        if not cls.get_task_namespace():
            return cls.__name__
        else:
            return "{}.{}".format(cls.get_task_namespace(), cls.__name__)

    @classmethod
    def get_params(cls):
        """
        Returns all of the Parameters for this Task.
        """
        # We want to do this here and not at class instantiation, or else there is no room to extend classes dynamically
        params = []
        for param_name in dir(cls):
            param_obj = getattr(cls, param_name)
            if not isinstance(param_obj, Parameter):
                continue

            params.append((param_name, param_obj))

        # The order the parameters are created matters. See Parameter class
        params.sort(key=lambda t: t[1]._counter)
        return params

    @classmethod
    def batch_param_names(cls):
        return [name for name, p in cls.get_params() if p._is_batchable()]

    @classmethod
    def get_param_names(cls, include_significant=False):
        return [name for name, p in cls.get_params() if include_significant or p.significant]

    @classmethod
    def get_param_values(cls, params, args, kwargs):
        """
        Get the values of the parameters from the args and kwargs.

        :param params: list of (param_name, Parameter).
        :param args: positional arguments
        :param kwargs: keyword arguments.
        :returns: list of `(name, value)` tuples, one for each parameter.
        """
        result = {}

        params_dict = dict(params)

        task_family = cls.get_task_family()

        # In case any exceptions are thrown, create a helpful description of how the Task was invoked
        # TODO: should we detect non-reprable arguments? These will lead to mysterious errors
        exc_desc = '%s[args=%s, kwargs=%s]' % (task_family, args, kwargs)

        # Fill in the positional arguments
        positional_params = [(n, p) for n, p in params if p.positional]
        for i, arg in enumerate(args):
            if i >= len(positional_params):
                raise parameter.UnknownParameterException('%s: takes at most %d parameters (%d given)' % (exc_desc, len(positional_params), len(args)))
            param_name, param_obj = positional_params[i]
            result[param_name] = param_obj.normalize(arg)

        # Then the keyword arguments
        for param_name, arg in kwargs.items():
            if param_name in result:
                raise parameter.DuplicateParameterException('%s: parameter %s was already set as a positional parameter' % (exc_desc, param_name))
            if param_name not in params_dict:
                raise parameter.UnknownParameterException('%s: unknown parameter %s' % (exc_desc, param_name))
            result[param_name] = params_dict[param_name].normalize(arg)

        # Then use the defaults for anything not filled in
        for param_name, param_obj in params:
            if param_name not in result:
                try:
                    has_task_value = param_obj.has_task_value(task_family, param_name)
                except Exception as exc:
                    raise ValueError("%s: Error when parsing the default value of '%s'" % (exc_desc, param_name)) from exc
                if not has_task_value:
                    raise parameter.MissingParameterException("%s: requires the '%s' parameter to be set" % (exc_desc, param_name))
                result[param_name] = param_obj.task_value(task_family, param_name)

        def list_to_tuple(x):
            """ Make tuples out of lists and sets to allow hashing """
            if isinstance(x, list) or isinstance(x, set):
                return tuple(x)
            else:
                return x

        # Check for unconsumed parameters
        conf = configuration.get_config()
        if not hasattr(cls, "_unconsumed_params"):
            cls._unconsumed_params = set()
        if task_family in conf.sections():
            ignore_unconsumed = getattr(cls, 'ignore_unconsumed', set())
            for key, value in conf[task_family].items():
                key = key.replace('-', '_')
                composite_key = f"{task_family}_{key}"
                if key not in result and key not in ignore_unconsumed and composite_key not in cls._unconsumed_params:
                    warnings.warn(
                        "The configuration contains the parameter "
                        f"'{key}' with value '{value}' that is not consumed by the task "
                        f"'{task_family}'.",
                        UnconsumedParameterWarning,
                    )
                    cls._unconsumed_params.add(composite_key)

        # Sort it by the correct order and make a list
        return [(param_name, list_to_tuple(result[param_name])) for param_name, param_obj in params]

    def __init__(self, *args, **kwargs):
        params = self.get_params()
        param_values = self.get_param_values(params, args, kwargs)

        # Set all values on class instance
        for key, value in param_values:
            setattr(self, key, value)

        # Register kwargs as an attribute on the class. Might be useful
        self.param_kwargs = dict(param_values)

        self._warn_on_wrong_param_types()
        self.task_id = task_id_str(self.get_task_family(), self.to_str_params(only_significant=True, only_public=True))
        self.__hash = hash(self.task_id)

        self.set_tracking_url = None
        self.set_status_message = None
        self.set_progress_percentage = None

    @property
    def param_args(self):
        warnings.warn("Use of param_args has been deprecated.", DeprecationWarning)
        return tuple(self.param_kwargs[k] for k, v in self.get_params())

    def initialized(self):
        """
        Returns ``True`` if the Task is initialized and ``False`` otherwise.
        """
        return hasattr(self, 'task_id')

    def _warn_on_wrong_param_types(self):
        params = dict(self.get_params())
        for param_name, param_value in self.param_kwargs.items():
            params[param_name]._warn_on_wrong_param_type(param_name, param_value)

    @classmethod
    def from_str_params(cls, params_str):
        """
        Creates an instance from a str->str hash.

        :param params_str: dict of param name -> value as string.
        """
        kwargs = {}
        for param_name, param in cls.get_params():
            if param_name in params_str:
                param_str = params_str[param_name]
                if isinstance(param_str, list):
                    kwargs[param_name] = param._parse_list(param_str)
                else:
                    kwargs[param_name] = param.parse(param_str)

        return cls(**kwargs)

    def to_str_params(self, only_significant=False, only_public=False):
        """
        Convert all parameters to a str->str hash.
        """
        params_str = {}
        params = dict(self.get_params())
        for param_name, param_value in self.param_kwargs.items():
            if (((not only_significant) or params[param_name].significant)
                    and ((not only_public) or params[param_name].visibility == ParameterVisibility.PUBLIC)
                    and params[param_name].visibility != ParameterVisibility.PRIVATE):
                params_str[param_name] = params[param_name].serialize(param_value)

        return params_str

    def _get_param_visibilities(self):
        param_visibilities = {}
        params = dict(self.get_params())
        for param_name, param_value in self.param_kwargs.items():
            if params[param_name].visibility != ParameterVisibility.PRIVATE:
                param_visibilities[param_name] = params[param_name].visibility.serialize()

        return param_visibilities

    def clone(self, cls=None, **kwargs):
        """
        Creates a new instance from an existing instance where some of the args have changed.

        There's at least two scenarios where this is useful (see test/clone_test.py):

        * remove a lot of boiler plate when you have recursive dependencies and lots of args
        * there's task inheritance and some logic is on the base class

        :param cls:
        :param kwargs:
        :return:
        """
        if cls is None:
            cls = self.__class__

        new_k = {}
        for param_name, param_class in cls.get_params():
            if param_name in kwargs:
                new_k[param_name] = kwargs[param_name]
            elif hasattr(self, param_name):
                new_k[param_name] = getattr(self, param_name)

        return cls(**new_k)

    def __hash__(self):
        return self.__hash

    def __repr__(self):
        """
        Build a task representation like `MyTask(param1=1.5, param2='5')`
        """
        params = self.get_params()
        param_values = self.get_param_values(params, [], self.param_kwargs)

        # Build up task id
        repr_parts = []
        param_objs = dict(params)
        for param_name, param_value in param_values:
            if param_objs[param_name].significant:
                repr_parts.append('%s=%s' % (param_name, param_objs[param_name].serialize(param_value)))

        task_str = '{}({})'.format(self.get_task_family(), ', '.join(repr_parts))

        return task_str

    def __eq__(self, other):
        return self.__class__ == other.__class__ and self.task_id == other.task_id

    def complete(self):
        """
        If the task has any outputs, return ``True`` if all outputs exist.
        Otherwise, return ``False``.

        However, you may freely override this method with custom logic.
        """
        outputs = flatten(self.output())
        if len(outputs) == 0:
            warnings.warn(
                "Task %r without outputs has no custom complete() method" % self,
                stacklevel=2
            )
            return False

        return all(map(lambda output: output.exists(), outputs))

    @classmethod
    def bulk_complete(cls, parameter_tuples):
        """
        Returns those of parameter_tuples for which this Task is complete.

        Override (with an efficient implementation) for efficient scheduling
        with range tools. Keep the logic consistent with that of complete().
        """
        raise BulkCompleteNotImplementedError()

    def output(self):
        """
        The output that this Task produces.

        The output of the Task determines if the Task needs to be run--the task
        is considered finished iff the outputs all exist. Subclasses should
        override this method to return a single :py:class:`Target` or a list of
        :py:class:`Target` instances.

        Implementation note
          If running multiple workers, the output must be a resource that is accessible
          by all workers, such as a DFS or database. Otherwise, workers might compute
          the same output since they don't see the work done by other workers.

        See :ref:`Task.output`
        """
        return []  # default impl

    def requires(self):
        """
        The Tasks that this Task depends on.

        A Task will only run if all of the Tasks that it requires are completed.
        If your Task does not require any other Tasks, then you don't need to
        override this method. Otherwise, a subclass can override this method
        to return a single Task, a list of Task instances, or a dict whose
        values are Task instances.

        See :ref:`Task.requires`
        """
        return []  # default impl

    def _requires(self):
        """
        Override in "template" tasks which themselves are supposed to be
        subclassed and thus have their requires() overridden (name preserved to
        provide consistent end-user experience), yet need to introduce
        (non-input) dependencies.

        Must return an iterable which among others contains the _requires() of
        the superclass.
        """
        return flatten(self.requires())  # base impl

    def process_resources(self):
        """
        Override in "template" tasks which provide common resource functionality
        but allow subclasses to specify additional resources while preserving
        the name for consistent end-user experience.
        """
        return self.resources  # default impl

    def input(self):
        """
        Returns the outputs of the Tasks returned by :py:meth:`requires`

        See :ref:`Task.input`

        :return: a list of :py:class:`Target` objects which are specified as
                 outputs of all required Tasks.
        """
        return getpaths(self.requires())

    def deps(self):
        """
        Internal method used by the scheduler.

        Returns the flattened list of requires.
        """
        # used by scheduler
        return flatten(self._requires())

    def run(self):
        """
        The task run method, to be overridden in a subclass.

        See :ref:`Task.run`
        """
        pass  # default impl

    def on_failure(self, exception):
        """
        Override for custom error handling.

        This method gets called if an exception is raised in :py:meth:`run`.
        The returned value of this method is json encoded and sent to the scheduler
        as the `expl` argument. Its string representation will be used as the
        body of the error email sent out if any.

        Default behavior is to return a string representation of the stack trace.
        """

        traceback_string = traceback.format_exc()
        return "Runtime error:\n%s" % traceback_string

    def on_success(self):
        """
        Override for doing custom completion handling for a larger class of tasks

        This method gets called when :py:meth:`run` completes without raising any exceptions.

        The returned value is json encoded and sent to the scheduler as the `expl` argument.

        Default behavior is to send an None value"""
        pass

    @contextmanager
    def no_unpicklable_properties(self):
        """
        Remove unpicklable properties before dump task and resume them after.

        This method could be called in subtask's dump method, to ensure unpicklable
        properties won't break dump.

        This method is a context-manager which can be called as below:

        .. code-block: python

            class DummyTask(simplui):

                def _dump(self):
                    with self.no_unpicklable_properties():
                        pickle.dumps(self)

        """
        unpicklable_properties = tuple(simplui.worker.TaskProcess.forward_reporter_attributes.values())
        reserved_properties = {}
        for property_name in unpicklable_properties:
            if hasattr(self, property_name):
                reserved_properties[property_name] = getattr(self, property_name)
                setattr(self, property_name, 'placeholder_during_pickling')

        yield

        for property_name, value in reserved_properties.items():
            setattr(self, property_name, value)


class MixinNaiveBulkComplete:
    """
    Enables a Task to be efficiently scheduled with e.g. range tools, by providing a bulk_complete implementation which checks completeness in a loop.

    Applicable to tasks whose completeness checking is cheap.

    This doesn't exploit output location specific APIs for speed advantage, nevertheless removes redundant scheduler roundtrips.
    """
    @classmethod
    def bulk_complete(cls, parameter_tuples):
        generated_tuples = []
        for parameter_tuple in parameter_tuples:
            if isinstance(parameter_tuple, (list, tuple)):
                if cls(*parameter_tuple).complete():
                    generated_tuples.append(parameter_tuple)
            elif isinstance(parameter_tuple, dict):
                if cls(**parameter_tuple).complete():
                    generated_tuples.append(parameter_tuple)
            else:
                if cls(parameter_tuple).complete():
                    generated_tuples.append(parameter_tuple)
        return generated_tuples


class DynamicRequirements(object):
    """
    Wraps dynamic requirements yielded in tasks's run methods to control how completeness checks of
    (e.g.) large chunks of tasks are performed. Besides the wrapped *requirements*, instances of
    this class can be passed an optional function *custom_complete* that might implement an
    optimized check for completeness. If set, the function will be called with a single argument,
    *complete_fn*, which should be used to perform the per-task check. Example:

    .. code-block:: python

        class SomeTaskWithDynamicRequirements(simplui.Task):
            ...

            def run(self):
                large_chunk_of_tasks = [OtherTask(i=i) for i in range(10000)]

                def custom_complete(complete_fn):
                    # example: assume OtherTask always write into the same directory, so just check
                    #          if the first task is complete, and compare basenames for the rest
                    if not complete_fn(large_chunk_of_tasks[0]):
                        return False
                    paths = [task.output().path for task in large_chunk_of_tasks]
                    basenames = os.listdir(os.path.dirname(paths[0]))  # a single fs call
                    return all(os.path.basename(path) in basenames for path in paths)

                yield DynamicRequirements(large_chunk_of_tasks, custom_complete)

    .. py:attribute:: requirements

        The original, wrapped requirements.

    .. py:attribute:: flat_requirements

        Flattened view of the wrapped requirements (via :py:func:`flatten`). Read only.

    .. py:attribute:: paths

        Outputs of the requirements in the identical structure (via :py:func:`getpaths`). Read only.

    .. py:attribute:: custom_complete

       The optional, custom function performing the completeness check of the wrapped requirements.
    """

    def __init__(self, requirements, custom_complete=None):
        super().__init__()

        # store attributes
        self.requirements = requirements
        self.custom_complete = custom_complete

        # cached flat requirements and paths
        self._flat_requirements = None
        self._paths = None

    @property
    def flat_requirements(self):
        if self._flat_requirements is None:
            self._flat_requirements = flatten(self.requirements)
        return self._flat_requirements

    @property
    def paths(self):
        if self._paths is None:
            self._paths = getpaths(self.requirements)
        return self._paths

    def complete(self, complete_fn=None):
        # default completeness check
        if complete_fn is None:
            def complete_fn(task):
                return task.complete()

        # use the custom complete function when set
        if self.custom_complete:
            return self.custom_complete(complete_fn)

        # default implementation
        return all(complete_fn(t) for t in self.flat_requirements)


class ExternalTask(Task):
    """
    Subclass for references to external dependencies.

    An ExternalTask's does not have a `run` implementation, which signifies to
    the framework that this Task's :py:meth:`output` is generated outside of
    simplui.
    """
    run = None


def externalize(taskclass_or_taskobject):
    """
    Returns an externalized version of a Task. You may both pass an
    instantiated task object or a task class. Some examples:

    .. code-block:: python

        class RequiringTask(simplui.Task):
            def requires(self):
                task_object = self.clone(MyTask)
                return externalize(task_object)

            ...

    Here's mostly equivalent code, but ``externalize`` is applied to a task
    class instead.

    .. code-block:: python

        @simplui.util.requires(externalize(MyTask))
        class RequiringTask(simplui.Task):
            pass
            ...

    Of course, it may also be used directly on classes and objects (for example
    for reexporting or other usage).

    .. code-block:: python

        MyTask = externalize(MyTask)
        my_task_2 = externalize(MyTask2(param='foo'))

    If you however want a task class to be external from the beginning, you're
    better off inheriting :py:class:`ExternalTask` rather than :py:class:`Task`.

    This function tries to be side-effect free by creating a copy of the class
    or the object passed in and then modify that object. In particular this
    code shouldn't do anything.

    .. code-block:: python

        externalize(MyTask)  # BAD: This does nothing (as after simplui 2.4.0)
    """
    copied_value = copy.copy(taskclass_or_taskobject)
    if copied_value is taskclass_or_taskobject:
        # Assume it's a class
        clazz = taskclass_or_taskobject

        @_task_wraps(clazz)
        class _CopyOfClass(clazz):
            # How to copy a class: http://stackoverflow.com/a/9541120/621449
            _visible_in_registry = False
        _CopyOfClass.run = None
        return _CopyOfClass
    else:
        # We assume it's an object
        copied_value.run = None
        return copied_value


class WrapperTask(Task):
    """
    Use for tasks that only wrap other tasks and that by definition are done if all their requirements exist.
    """

    def complete(self):
        return all(r.complete() for r in flatten(self.requires()))


class Config(Task):
    """
    Class for configuration. See :ref:`ConfigClasses`.
    """
    # TODO: let's refactor Task & Config so that it inherits from a common
    # ParamContainer base class
    pass


def getpaths(struct):
    """
    Maps all Tasks in a structured data object to their .output().
    """
    if isinstance(struct, Task):
        return struct.output()
    elif isinstance(struct, dict):
        return struct.__class__((k, getpaths(v)) for k, v in struct.items())
    elif isinstance(struct, (list, tuple)):
        return struct.__class__(getpaths(r) for r in struct)
    else:
        # Remaining case: assume struct is iterable...
        try:
            return [getpaths(r) for r in struct]
        except TypeError:
            raise Exception('Cannot map %s to Task/dict/list' % str(struct))


def flatten(struct):
    """
    Creates a flat list of all items in structured output (dicts, lists, items):

    .. code-block:: python

        >>> sorted(flatten({'a': 'foo', 'b': 'bar'}))
        ['bar', 'foo']
        >>> sorted(flatten(['foo', ['bar', 'troll']]))
        ['bar', 'foo', 'troll']
        >>> flatten('foo')
        ['foo']
        >>> flatten(42)
        [42]
    """
    if struct is None:
        return []
    flat = []
    if isinstance(struct, dict):
        for _, result in struct.items():
            flat += flatten(result)
        return flat
    if isinstance(struct, str):
        return [struct]

    try:
        # if iterable
        iterator = iter(struct)
    except TypeError:
        return [struct]

    for result in iterator:
        flat += flatten(result)
    return flat


def flatten_output(task):
    """
    Lists all output targets by recursively walking output-less (wrapper) tasks.
    """

    output_tasks = OrderedDict()  # OrderedDict used as ordered set
    tasks_to_process = deque([task])
    while tasks_to_process:
        current_task = tasks_to_process.popleft()
        if flatten(current_task.output()):
            if current_task not in output_tasks:
                output_tasks[current_task] = None
        else:
            tasks_to_process.extend(flatten(current_task.requires()))

    return flatten(task.output() for task in output_tasks)


def _task_wraps(task_class):
    # In order to make the behavior of a wrapper class nicer, we set the name of the
    # new class to the wrapped class, and copy over the docstring and module as well.
    # This makes it possible to pickle the wrapped class etc.
    # Btw, this is a slight abuse of functools.wraps. It's meant to be used only for
    # functions, but it works for classes too, if you pass updated=[]
    assigned = functools.WRAPPER_ASSIGNMENTS + ('_namespace_at_class_time',)
    return functools.wraps(task_class, assigned=assigned, updated=[])
