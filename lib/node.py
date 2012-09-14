#!/usr/bin/env python
# vim: set encoding=utf-8 tabstop=4 softtabstop=4 shiftwidth=4 expandtab
#########################################################################
# Copyright 2012 KNX-User-Forum e.V             http://knx-user-forum.de/
#########################################################################
#  This file is part of SmartHome.py.
#
#  SmartHome.py is free software: you can redistribute it and/or modify
#  it under the terms of the GNU General Public License as published by
#  the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  SmartHome.py is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with SmartHome.py. If not, see <http://www.gnu.org/licenses/>.
#########################################################################

import logging
import configobj
import datetime
import threading
import time
import cPickle

logger = logging.getLogger('')


class Node():
    _defaults = {'num': 0, 'str': '', 'bool': False, 'list': [], 'foo': None}
    def __init__(self, smarthome, root, path, config):
        # basic attributes
        self._path = path
        self._name = path
        self._value = None
        self._type = None
        # public attributes
        self.conf = {}
        self.last_change = smarthome.now()
        self.prev_change = self.last_change
        self.changed_by = 'Init'
        # special attributes
        self._sh = smarthome
        self._lock = threading.Condition()
        self._cache = False
        self._eval = False
        self._threshold = False
        self._enforce_updates = False
        self._parent = root
        self._sub_nodes = []
        self._plugins_to_trigger = []
        self._logics_to_trigger = []
        self._nodes_to_trigger = []
        self.__fade = False
        # parse config
        for attr in config:
            if isinstance(config[attr], dict): # sub node
                sub_path = self._path + '.' + attr
                sub_node = Node(smarthome, self, sub_path, config[attr])
                vars(self)[attr] = sub_node
                smarthome.add_node(sub_path, sub_node)
                self._sub_nodes.append(sub_node)
            else: # attribute
                if attr == 'type':
                    self._type = config[attr]
                elif attr == 'value':
                    self._value = config[attr]
                elif attr == 'name':
                    self._name = config[attr]
                elif attr == 'cache':
                    self._cache = self._return_bool(config[attr])
                elif attr == 'enforce_updates':
                    self._enforce_updates = self._return_bool(config[attr])
                elif attr == 'threshold':
                    self._threshold = config[attr]
                elif attr == 'eval':
                    self._eval = config[attr]
                elif attr == 'eval_trigger':
                    if isinstance(config[attr], str):
                        self.conf[attr] = [config[attr], ]
                    else:
                        self.conf[attr] = config[attr]
                else:
                    self.conf[attr] = config[attr]
        if self._type != None:
            if self._value == None:
                self._value = self._defaults[self._type]
            else:
                try:
                    self._value = getattr(self, '_return_' + self._type)(self._value)
                except:
                    logger.error("Node '{0}': value ({1}) does not match type ({2}). Ignoring!".format(path, self._value, self._type))
                    return
        else:
            logger.debug("Node '{0}': No type specified.".format(self._path))
            return
        if self._cache:
            self._value = self._db_read()
        if self._eval and 'eval_trigger' in self.conf:
            nodes = map(lambda x: 'sh.' + x + '()', self.conf['eval_trigger'])
            if self._eval == 'and':
                self._eval = ' and '.join(nodes)
            elif self._eval == 'or':
                self._eval = ' or '.join(nodes)
            elif self._eval == 'sum':
                self._eval = ' + '.join(nodes)
            elif self._eval == 'avg':
                self._eval = '({0})/{1}'.format(' + '.join(nodes), len(nodes))
        if self._threshold:
            low, sep, high = self._threshold.rpartition(':')
            if not low:
                low = high
            self._th_low = float(low)
            self._th_high = float(high)
            logger.debug("Node '{0}': set threshold => low: {1} high: {2}".format(self._path, self._th_low, self._th_high))
            self._th = False
        for plugin in self._sh.return_plugins():
            if hasattr(plugin, 'parse_node'):
                update = plugin.parse_node(self)
                if update:
                    self._plugins_to_trigger.append(update)

    def init_eval_trigger(self):
        if 'eval_trigger' in self.conf:
            for node in self.conf['eval_trigger']:
                node = self._sh.return_node(node)
                if node != None:
                    node._nodes_to_trigger.append(self)

    def init_eval_run(self):
        if 'eval_trigger' in self.conf:
            if self._eval:
                self._sh.trigger(name=self._path, obj=self._run_eval, by='Init')
            del(self.conf['eval_trigger'])

    def _run_eval(self, value=None, caller='Eval', source=None):
        if self._eval:
            sh = self._sh
            value = eval(self._eval)
            self._update(value, caller, source)

    def __call__(self, value=None, caller='Logic', source=None):
        if value == None:
            return self._value
        try:
            value = getattr(self, '_return_' + self._type)(value)
        except:
            logger.error("Node '{0}': value ({1}) does not match type ({2}). Via {3} {4}".format(self._path, value, self._type, caller, source))
            return
        if self._eval:
            args = {'value': value, 'caller': caller, 'source': source}
            self._sh.trigger(name=self._path + '-eval', obj=self._run_eval, value=args, by=caller, source=source)
        else:
            self._update(value, caller, source)

    def _update(self, value=None, caller='Logic', source=None):
        try:
            value = getattr(self, '_return_' + self._type)(value)
        except:
            logger.error("Node '{0}': value ({1}) does not match type ({2}). Via {3}  {4}".format(self._path, value, self._type, caller, source))
            return
        self._lock.acquire()

        if value != self._value or self._enforce_updates: # value change
            #logger.debug("update item: %s" % self._path)
            if caller != "fade":
                self.__fade = False
                self._lock.notify_all()
                logger.info("{0} = {1} via {2} {3}".format(self._path, value, caller, source))
            self._value = value
            delta = self._sh.now() - self.last_change
            self.prev_change = delta.seconds + delta.days * 24 * 3600 # FIXME change to timedelta.total_seconds()
            self.last_change = self._sh.now()
            self.changed_by = "{0}:{1}".format(caller, source)
            self._lock.release()
            for update_plugin in self._plugins_to_trigger:
                try:
                    update_plugin(self, caller, source)
                except Exception, e:
                    logger.error("Problem running {0}: {1}".format(update_plugin, e))

            if self._threshold and self._logics_to_trigger:
                if self._th and self._value <= self._th_low: # cross lower bound
                    self._th = False
                    self._trigger_logics()
                elif not self._th and self._value >= self._th_high: # cross upper bound
                    self._th = True
                    self._trigger_logics()
            elif self._logics_to_trigger:
                self._trigger_logics()
            for node in self._nodes_to_trigger:
                args = {'value': value, 'source': self._path}
                self._sh.trigger(name=node.id(), obj=node._run_eval, value=args, by=caller, source=source)
            if self._cache and not self.__fade: self._db_update(value)
        else:
            self._lock.release()

    def __iter__(self):
        for node in self._sub_nodes:
            yield node

    def return_children(self):
        for node in self._sub_nodes:
            yield node

    def return_parent(self):
        return self._parent

    def __setitem__(self, item, value):
        vars(self)[item] = value

    def __getitem__(self, item):
        return vars(self)[item]

    def _db_read(self):
        try:
            with open(self._sh._cache_dir + self._path, 'r') as f:
                return cPickle.load(f)
        except IOError, e:
            logger.info("Could not read {0}{1}".format(self._sh._cache_dir, self._path))
            try:
                return getattr(self, '_return_' + self._type)(0)
            except:
                return False
        except EOFError, e:
            logger.info("{0}{1} is empty".format(self._sh._cache_dir, self._path))
            try:
                return getattr(self, '_return_' + self._type)(0)
            except:
                return False

    def _db_update(self, value):
        try:
            with open(self._sh._cache_dir + self._path, 'w') as f:
                cPickle.dump(value,f)
        except IOError, e:
            logger.warning("Could not write to {0}{1}".format(self._sh._cache_dir, self._path))

    def __cmp__(self, compare):
        return cmp(self._value, compare)

    def id(self):
        return self._path

    def __nonzero__(self):
        return self._value

    def __str__(self):
        return self._name

    def __repr__(self):
        return "Node: {0}".format(self._value)

    def add_logic_trigger(self, logic):
        self._logics_to_trigger.append(logic)

    def _trigger_logics(self):
        for logic in self._logics_to_trigger:
            logic.trigger('Node', self._path, self._value)

    def _return_str(self, value):
        if isinstance(value, basestring):
            return value
        else:
            raise ValueError

    def _return_list(self, value):
        if isinstance(value, list):
            return value
        else:
            raise ValueError

    def _return_foo(self, value):
        return value

    def _return_bool(self, value):
        if type(value) in [bool, int, long, float]:
            if value in [False, 0]:
                return False
            elif value in [True, 1]:
                return True
            else:
                raise ValueError
        elif type(value) in [str, unicode]:
            if value.lower() in ['0', 'false', 'no', 'off']:
                return False
            elif value.lower() in ['1', 'true', 'yes', 'on']:
                return True
            else:
                raise ValueError
        else:
            raise TypeError

    def _return_num(self, value):
        if isinstance(value, float):
            return value

        try:
            return int(value)
        except:
            pass

        try:
            return float(value)
        except:
            pass

        raise ValueError

    def fade(self, dest, step=1, delta=1):
        dest = float(dest)
        self._sh.trigger('fade', self.__fadejob, value=(dest, step, delta))

    def _fadejob(self, (dest, step, delta)):
        self.__fade = True
        if self._value < dest:
            while (self._value + step) < dest and self.__fade:
                self(self._value + step, 'fade')
                self._lock.acquire()
                self._lock.wait(delta)
                self._lock.release()
        else:
            while (self._value - step) > dest and self.__fade:
                self(self._value - step, 'fade')
                self._lock.acquire()
                self._lock.wait(delta)
                self._lock.release()

        if self.__fade:
            self(dest)

