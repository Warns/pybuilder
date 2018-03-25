#   -*- coding: utf-8 -*-
#
#   This file is part of PyBuilder
#
#   Copyright 2011-2015 PyBuilder Team
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

"""
    The PyBuilder execution module.
    Deals with the execution of a PyBuilder process by
    running tasks, actions and initializers in the correct
    order regarding dependencies.
"""

import copy
import inspect
import re
import sys
import traceback
import types

from pybuilder.errors import (CircularTaskDependencyException,
                              DependenciesNotResolvedException,
                              InvalidNameException,
                              MissingTaskDependencyException,
                              MissingActionDependencyException,
                              NoSuchTaskException,
                              RequiredTaskExclusionException)
from pybuilder.graph_utils import Graph
from pybuilder.utils import as_list, Timer, odict

if sys.version_info[0] < 3:  # if major is less than 3
    from .excp_util_2 import raise_exception

    getargspec = inspect.getargspec
else:
    from .excp_util_3 import raise_exception

    getargspec = inspect.getfullargspec


def as_task_name(item):
    if isinstance(item, types.FunctionType):
        return item.__name__
    elif hasattr(item, "name"):
        return item.name
    else:
        return str(item)


def as_task_name_list(mixed):
    result = []
    for item in as_list(mixed):
        result.append(as_task_name(item))
    return result


class Executable(object):
    NAME_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]+$")

    def __init__(self, name, callable, description=""):
        if not Executable.NAME_PATTERN.match(name):
            raise InvalidNameException(name)

        self._name = name
        self.description = description
        self.callable = callable
        if hasattr(callable, "__module__"):
            self.source = callable.__module__
        else:
            self.source = "n/a"

        if isinstance(self.callable, types.FunctionType):
            self.parameters = getargspec(self.callable).args
        else:
            raise TypeError("Don't know how to handle callable %s" % callable)

    @property
    def name(self):
        return self._name

    def execute(self, argument_dict):
        arguments = []
        for parameter in self.parameters:
            if parameter not in argument_dict:
                raise ValueError("Invalid parameter '%s' for %s %s" % (parameter, self.__class__.__name__, self.name))
            arguments.append(argument_dict[parameter])

        self.callable(*arguments)


class Action(Executable):
    def __init__(self, name, callable, before=None, after=None, description="", only_once=False, teardown=False):
        super(Action, self).__init__(name, callable, description)
        self.execute_before = as_task_name_list(before)
        self.execute_after = as_task_name_list(after)
        self.only_once = only_once
        self.teardown = teardown


class TaskDependency(object):
    def __init__(self, mixed, optional=False):
        self._name = as_task_name(mixed)
        self._task = mixed if hasattr(mixed, "name") else None
        self._optional = optional

    def __repr__(self):
        return self._name if not self._optional else self._name + "(optional)"

    def __eq__(self, other):
        if isinstance(other, TaskDependency):
            return self._name == other._name and self._optional == other._optional

    @property
    def name(self):
        return self._name

    @property
    def task(self):
        return self._task

    @property
    def optional(self):
        return self._optional


class Task(object):
    def __init__(self, name, callable, dependencies=None, description=""):
        self.name = name
        self.executables = [Executable(name, callable, description)]
        self.dependencies = as_list(dependencies)
        self.description = [description]

    def __eq__(self, other):
        if isinstance(other, Task):
            return self.name == other.name
        return False

    def __hash__(self):
        return 9 * hash(self.name)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __lt__(self, other):
        if isinstance(other, Task):
            return self.name < other.name
        return self.name < other

    def extend(self, task):
        self.executables += task.executables
        self.dependencies += task.dependencies
        self.description += task.description

    def execute(self, logger, argument_dict):
        for executable in self.executables:
            logger.debug("Executing subtask from %s", executable.source)
            executable.execute(argument_dict)


class Initializer(Executable):
    def __init__(self, name, callable, environments=None, description=""):
        super(Initializer, self).__init__(name, callable, description)
        self.environments = environments

    def is_applicable(self, environments=None):
        if not self.environments:
            return True
        for environment in as_list(environments):
            if environment in self.environments:
                return True


class TaskExecutionSummary(object):
    def __init__(self, task, number_of_actions, execution_time):
        self.task = task
        self.number_of_actions = number_of_actions
        self.execution_time = execution_time


class ExecutionManager(object):
    def __init__(self, logger):
        self.logger = logger

        self._tasks = odict()
        self._task_dependencies = odict()
        self._dependencies_pending_tasks = {}

        self._actions = odict()
        self._execute_before = odict()
        self._execute_after = odict()

        self._initializers = []

        self._dependencies_resolved = False
        self._actions_executed = []
        self._tasks_executed = []
        self._current_task = None
        self._current_execution_plan = None

        self._exclude_optional_tasks = []
        self._exclude_tasks = []
        self._exclude_all_optional = False

    @property
    def initializers(self):
        return self._initializers

    @property
    def tasks(self):
        return list(self._tasks.values())

    @property
    def task_names(self):
        return sorted(self._tasks.keys())

    def register_initializer(self, initializer):
        self.logger.debug("Registering initializer '%s'", initializer.name)
        self._initializers.append(initializer)

    def register_action(self, action):
        self.logger.debug("Registering action '%s'", action.name)
        self._actions[action.name] = action

    def register_task(self, *tasks):
        for task in tasks:
            self.logger.debug("Registering task '%s'", task.name)
            if task.name in self._tasks:
                self._tasks[task.name].extend(task)
            else:
                self._tasks[task.name] = task

    def register_late_task_dependencies(self, dependencies):
        for name in dependencies:
            self.logger.debug("Registering late dependency of task '%s' on %s", name, dependencies[name])
            if name not in self._dependencies_pending_tasks:
                self._dependencies_pending_tasks[name] = []
            self._dependencies_pending_tasks[name].extend(dependencies[name])

        for name in list(self._dependencies_pending_tasks.keys()):
            if self.has_task(name):
                self.logger.debug("Resolved late dependency of task '%s' on %s", name, dependencies[name])
                self.get_task(name).dependencies.extend(self._dependencies_pending_tasks[name])
                del self._dependencies_pending_tasks[name]

    def execute_initializers(self, environments=None, **keyword_arguments):
        for initializer in self._initializers:
            if not initializer.is_applicable(environments):
                message = "Not going to execute initializer '%s' from '%s' as environments do not match."
                self.logger.debug(message, initializer.name, initializer.source)

            else:
                self.logger.debug("Executing initializer '%s' from '%s'",
                                  initializer.name, initializer.source)
                initializer.execute(keyword_arguments)

    def assert_dependencies_resolved(self):
        if not self._dependencies_resolved:
            raise DependenciesNotResolvedException()

    def execute_task(self, task, **keyword_arguments):
        self.assert_dependencies_resolved()

        self.logger.debug("Executing task '%s'",
                          task.name)

        timer = Timer.start()
        number_of_actions = 0

        self._current_task = task

        suppressed_errors = []
        task_error = None

        has_teardown_tasks = False
        after_actions = self._execute_after[task.name]
        for action in after_actions:
            if action.teardown:
                has_teardown_tasks = True
                break

        try:
            for action in self._execute_before[task.name]:
                if self.execute_action(action, keyword_arguments):
                    number_of_actions += 1

            task.execute(self.logger, keyword_arguments)
        except:
            if not has_teardown_tasks:
                raise
            else:
                task_error = sys.exc_info()

        for action in after_actions:
            try:
                if not task_error or action.teardown:
                    if self.execute_action(action, keyword_arguments):
                        number_of_actions += 1
            except:
                if not has_teardown_tasks:
                    raise
                elif task_error:
                    suppressed_errors.append((action, sys.exc_info()))
                else:
                    task_error = sys.exc_info()

        for suppressed_error in suppressed_errors:
            action = suppressed_error[0]
            action_error = suppressed_error[1]
            self.logger.error("Executing action '%s' from '%s' resulted in an error that was suppressed:\n%s",
                              action.name, action.source,
                              "".join(traceback.format_exception(action_error[0], action_error[1], action_error[2])))
        if task_error:
            raise_exception(task_error[1], task_error[2])
        self._current_task = None
        if task not in self._tasks_executed:
            self._tasks_executed.append(task)

        timer.stop()
        return TaskExecutionSummary(task.name, number_of_actions, timer.get_millis())

    def execute_action(self, action, arguments):
        if action.only_once and action in self._actions_executed:
            message = "Action %s has been executed before and is marked as only_once, so will not be executed again"
            self.logger.debug(message, action.name)
            return False

        self.logger.debug("Executing action '%s' from '%s' before task", action.name, action.source)
        action.execute(arguments)
        self._actions_executed.append(action)
        return True

    def execute_execution_plan(self, execution_plan, **keyword_arguments):
        self.assert_dependencies_resolved()

        summaries = []
        self._current_execution_plan = execution_plan
        try:
            for task in execution_plan:
                summaries.append(self.execute_task(task, **keyword_arguments))
        finally:
            self._current_execution_plan = None

        return summaries

    def get_task(self, name):
        name = name.name if isinstance(name, TaskDependency) else name
        if not self.has_task(name):
            raise NoSuchTaskException(name)
        return self._tasks[name]

    def has_task(self, name):
        name = name.name if isinstance(name, TaskDependency) else name
        return name in self._tasks

    def _collect_transitive_tasks(self, task, visited=None):
        if not visited:
            visited = set()
        if task in visited:
            return visited
        visited.add(task)
        dependencies = [dependency for dependency in self._task_dependencies[task.name]]
        for dependency in dependencies:
            self._collect_transitive_tasks(dependency.task, visited)
        return visited

    def collect_all_transitive_tasks(self, task_names):
        self.assert_dependencies_resolved()

        all_tasks = set()
        for task_name in task_names:
            all_tasks.update(self._collect_transitive_tasks(self.get_task(task_name)))
        return all_tasks

    def build_execution_plan(self, task_names):
        self.assert_dependencies_resolved()

        execution_plan = []

        dependency_edges = {}
        for task in self.collect_all_transitive_tasks(as_list(task_names)):
            dependency_edges[task.name] = [dependency.name for dependency in task.dependencies]

        cycles = Graph(dependency_edges).assert_no_cycles_present()
        if cycles:
            raise CircularTaskDependencyException(cycles)

        for task_name in as_list(task_names):
            self._enqueue_task(execution_plan, task_name)
        return execution_plan

    def build_shortest_execution_plan(self, task_names):
        """
        Finds the shortest execution plan taking into the account tasks already executed
        This is useful when you want to execute tasks dynamically without repeating pre-requisite
        tasks you've already executed
        """
        execution_plan = self.build_execution_plan(task_names)
        shortest_plan = copy.copy(execution_plan)
        for executed_task in self._tasks_executed:
            candidate_task = shortest_plan[0]
            if candidate_task.name not in task_names and candidate_task == executed_task:
                shortest_plan.pop(0)
            else:
                break

        if self._current_task and self._current_task in shortest_plan:
            raise CircularTaskDependencyException("Task '%s' attempted to invoke tasks %s, "
                                                  "resulting in plan %s, creating circular dependency",
                                                  self._current_task, task_names, shortest_plan)
        return shortest_plan

    def _enqueue_task(self, execution_plan, task_name):
        task = self.get_task(task_name)

        if task in execution_plan:
            return

        for dependency in self._task_dependencies[task.name]:
            if self._should_omit_dependency(task, dependency):
                continue
            self._enqueue_task(execution_plan, dependency.name)

        execution_plan.append(task)

    def _should_omit_dependency(self, task, dependency):
        if dependency.optional:
            if self._exclude_all_optional or \
                    dependency.name in self._exclude_optional_tasks or \
                    dependency.name in self._exclude_tasks:
                self.logger.debug("Omitting optional dependency '%s' of task '%s'", dependency.name, task.name)
                return True
        else:
            if dependency.name in self._exclude_optional_tasks:
                raise RequiredTaskExclusionException(task.name, dependency.name)
            if dependency.name in self._exclude_tasks:
                self.logger.warn("Omitting required dependency '%s' of task '%s'", dependency.name, task.name)
                return True
        return False

    def resolve_dependencies(self, exclude_optional_tasks=None, exclude_tasks=None, exclude_all_optional=False):
        self._exclude_optional_tasks = as_task_name_list(exclude_optional_tasks or [])
        self._exclude_tasks = as_task_name_list(exclude_tasks or [])
        self._exclude_all_optional = exclude_all_optional

        self.register_late_task_dependencies({})  # This tries to flush out all remaining pending dependencies
        for name in self._dependencies_pending_tasks:
            self.get_task(name)
        self._dependencies_pending_tasks.clear()

        for task in self._tasks.values():
            self._execute_before[task.name] = []
            self._execute_after[task.name] = []
            self._task_dependencies[task.name] = []

            for d in task.dependencies:
                add_dependency = True
                if not self.has_task(d):
                    raise MissingTaskDependencyException(task.name, d)
                task_dependencies = self._task_dependencies[task.name]
                for index, existing_dependency in enumerate(task_dependencies):
                    if existing_dependency.name == d.name:
                        if existing_dependency.optional != d.optional:
                            if existing_dependency.optional:
                                task_dependencies[index] = TaskDependency(existing_dependency.name)
                                self.logger.debug("Converting optional dependency '%s' of task '%s' into required",
                                                  existing_dependency, task.name)
                            else:
                                self.logger.debug(
                                    "Ignoring '%s' as optional dependency of task '%s' - already required",
                                    existing_dependency, task.name)
                        add_dependency = False
                if add_dependency:
                    self._task_dependencies[task.name].append(TaskDependency(self.get_task(d), d.optional))
                    self.logger.debug("Adding '%s' as a dependency of task '%s'", d, task.name)

        for action in self._actions.values():
            for task in action.execute_before:
                if not self.has_task(task):
                    raise MissingActionDependencyException(action.name, task)
                self._execute_before[task].append(action)
                self.logger.debug("Adding before action '%s' for task '%s'", action.name, task)

            for task in action.execute_after:
                if not self.has_task(task):
                    raise MissingActionDependencyException(action.name, task)
                self._execute_after[task].append(action)
                self.logger.debug("Adding after action '%s' for task '%s'", action.name, task)

        self._dependencies_resolved = True

    def is_task_in_current_execution_plan(self, task_name):
        if self._current_execution_plan:
            for task in self._current_execution_plan:
                if task.name == task_name:
                    return True
        return False
