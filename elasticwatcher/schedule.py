#!/usr/bin/env python
# -*- coding: utf-8 -*-

import thread
import threading

from util import elastalert_logger
from util import intervalInSecond

class Operation(threading._Timer):
    def __init__(self, interval, operation, *args, **kwargs):
        threading._Timer.__init__(self, interval, operation, *args, **kwargs)
        self.setDaemon(True)
        self.function = operation
        self.interval = interval

    def run(self):
        while True:
            self.finished.clear()
            self.finished.wait(self.interval)
            if not self.finished.isSet():
                self.function(*self.args, **self.kwargs)
            else:
                return
            self.finished.set()

class Manager(object):

    ops = []

    def add_operation(self, operation, interval, args=[], kwargs={}):
        op = Operation(interval, operation, args, kwargs)
        self.ops.append(op)
        thread.start_new_thread(op.run, ())

    def stop(self):
        for op in self.ops:
            op.cancel()
        #self._event.set()

