#!/usr/bin/env python
import pickle
import functools
import inspect
import linecache
import tempfile
import os
import sys
from argparse import ArgumentError, ArgumentParser
from typing import List, Sequence

try:
    from ._line_profiler import LineProfiler as CLineProfiler
    from ._line_profiler import LineStats
except ImportError as ex:
    raise ImportError(
        'The line_profiler._line_profiler c-extension is not importable. '
        f'Has it been compiled? Underlying error is ex={ex!r}'
    )

# NOTE: This needs to be in sync with ../kernprof.py
__version__ = '4.0.4'


def load_ipython_extension(ip):
    """ API for IPython to recognize this module as an IPython extension.
    """
    from .ipython_extension import LineProfilerMagics
    ip.register_magics(LineProfilerMagics)


def is_coroutine(f):
    return inspect.iscoroutinefunction(f)


CO_GENERATOR = 0x0020


def is_generator(f):
    """ Return True if a function is a generator.
    """
    isgen = (f.__code__.co_flags & CO_GENERATOR) != 0
    return isgen


def is_classmethod(f):
    return isinstance(f, classmethod)


class LineProfiler(CLineProfiler):
    """ A profiler that records the execution times of individual lines.
    """

    def __call__(self, func):
        """ Decorate a function to start the profiler on function entry and stop
        it on function exit.
        """
        self.add_function(func)
        if is_classmethod(func):
            wrapper = self.wrap_classmethod(func)
        elif is_coroutine(func):
            wrapper = self.wrap_coroutine(func)
        elif is_generator(func):
            wrapper = self.wrap_generator(func)
        else:
            wrapper = self.wrap_function(func)
        return wrapper

    def wrap_classmethod(self, func):
        """
        Wrap a classmethod to profile it.
        """
        @functools.wraps(func)
        def wrapper(*args, **kwds):
            self.enable_by_count()
            try:
                result = func.__func__(func.__class__, *args, **kwds)
            finally:
                self.disable_by_count()
            return result
        return wrapper

    def wrap_coroutine(self, func):
        """
        Wrap a Python 3.5 coroutine to profile it.
        """

        @functools.wraps(func)
        async def wrapper(*args, **kwds):
            self.enable_by_count()
            try:
                result = await func(*args, **kwds)
            finally:
                self.disable_by_count()
            return result

        return wrapper

    def wrap_generator(self, func):
        """ Wrap a generator to profile it.
        """
        @functools.wraps(func)
        def wrapper(*args, **kwds):
            g = func(*args, **kwds)
            # The first iterate will not be a .send()
            self.enable_by_count()
            try:
                item = next(g)
            except StopIteration:
                return
            finally:
                self.disable_by_count()
            input_ = (yield item)
            # But any following one might be.
            while True:
                self.enable_by_count()
                try:
                    item = g.send(input_)
                except StopIteration:
                    return
                finally:
                    self.disable_by_count()
                input_ = (yield item)
        return wrapper

    def wrap_function(self, func):
        """ Wrap a function to profile it.
        """
        @functools.wraps(func)
        def wrapper(*args, **kwds):
            self.enable_by_count()
            try:
                result = func(*args, **kwds)
            finally:
                self.disable_by_count()
            return result
        return wrapper

    def dump_stats(self, filename):
        """ Dump a representation of the data to a file as a pickled LineStats
        object from `get_stats()`.
        """
        lstats = self.get_stats()
        with open(filename, 'wb') as f:
            pickle.dump(lstats, f, pickle.HIGHEST_PROTOCOL)

    def print_stats(self, stream=None, output_unit=None, stripzeros=False):
        """ Show the gathered statistics.
        """
        lstats = self.get_stats()
        show_text(lstats.timings, lstats.unit, output_unit=output_unit, stream=stream, stripzeros=stripzeros)

    def run(self, cmd):
        """ Profile a single executable statment in the main namespace.
        """
        import __main__
        main_dict = __main__.__dict__
        return self.runctx(cmd, main_dict, main_dict)

    def runctx(self, cmd, globals, locals):
        """ Profile a single executable statement in the given namespaces.
        """
        self.enable_by_count()
        try:
            exec(cmd, globals, locals)
        finally:
            self.disable_by_count()
        return self

    def runcall(self, func, *args, **kw):
        """ Profile a single function call.
        """
        self.enable_by_count()
        try:
            return func(*args, **kw)
        finally:
            self.disable_by_count()

    def add_module(self, mod):
        """ Add all the functions in a module and its classes.
        """
        from inspect import isclass, isfunction

        nfuncsadded = 0
        for item in mod.__dict__.values():
            if isclass(item):
                for k, v in item.__dict__.items():
                    if isfunction(v):
                        self.add_function(v)
                        nfuncsadded += 1
            elif isfunction(item):
                self.add_function(item)
                nfuncsadded += 1

        return nfuncsadded


# This could be in the ipython_extension submodule,
# but it doesn't depend on the IPython module so it's easier to just let it stay here.
def is_ipython_kernel_cell(filename):
    """ Return True if a filename corresponds to a Jupyter Notebook cell
    """
    return (
        filename.startswith('<ipython-input-') or
        filename.startswith(os.path.join(tempfile.gettempdir(), 'ipykernel_')) or
        filename.startswith(os.path.join(tempfile.gettempdir(), 'xpython_'))
    )


def show_func(filename, start_lineno, func_name, timings, unit,
              output_unit=None, stream=None, stripzeros=False):
    """
    Show results for a single function.

    Args:
        filename (str): path to the profiled file
        start_lineno (int): first line number of profiled function
        func_name (str): name of profiled function
        timings (List[Tuple[int, int, float]]): measurements for each line
        unit (float):
        output_unit (float | None):
        stream (io.TextIO | None): defaults to sys.stdout
        stripzeros (bool):

    Example:
        >>> from line_profiler.line_profiler import *  # NOQA
        >>> import line_profiler
        >>> # Use a function in this file as an example
        >>> func = line_profiler.line_profiler.show_text
        >>> start_lineno = func.__code__.co_firstlineno
        >>> func_lines = list(func.__code__.co_lines())
        >>> filename = func.__code__.co_filename
        >>> func_name = func.__name__
        >>> # Build fake timeings for each line in the example function
        >>> timings = [
        >>>     (line_tup[2], idx * 1e13, idx * 2e9)
        >>>     for idx, line_tup in enumerate(func_lines, start=1)
        >>> ]
        >>> unit = 1.0
        >>> output_unit = 1.0
        >>> stream = None
        >>> stripzeros = False
        >>> show_func(filename, start_lineno, func_name, timings, unit,
        >>>           output_unit, stream, stripzeros)
    """
    import math
    if stream is None:
        stream = sys.stdout

    template = '%6s %9s %12s %8s %8s  %-s'
    d = {}
    total_time = 0.0
    linenos = []
    max_hits = 0
    max_time = 0
    for lineno, nhits, time in timings:
        total_time += time
        max_hits = max(nhits, max_hits)
        max_time = max(time, max_time)
        linenos.append(lineno)

    # Define how large to make each column so text reasonably fits.
    column_sizes = {
        'line': 6,
        'hits': 9,
        'time': 12,
        'perhit': 8,
        'percent': 8,
    }
    col_order = ['line', 'hits', 'time', 'perhit', 'percent']
    hit_ndigits = int(math.log10(max(max_hits, 1))) + 3
    time_ndigits = int(math.log10(max(max_time, 1))) + 3
    column_sizes['hits'] = max(column_sizes['hits'], hit_ndigits)
    column_sizes['time'] = max(column_sizes['time'], time_ndigits)
    template = ' '.join(['%' + str(column_sizes[k]) + 's' for k in col_order])
    template = template + '  %-s'

    if stripzeros and total_time == 0:
        return

    if output_unit is None:
        output_unit = unit
    scalar = unit / output_unit

    stream.write('Total time: %g s\n' % (total_time * unit))
    if os.path.exists(filename) or is_ipython_kernel_cell(filename):
        stream.write(f'File: {filename}\n')
        stream.write(f'Function: {func_name} at line {start_lineno}\n')
        if os.path.exists(filename):
            # Clear the cache to ensure that we get up-to-date results.
            linecache.clearcache()
        all_lines = linecache.getlines(filename)
        sublines = inspect.getblock(all_lines[start_lineno - 1:])
    else:
        stream.write('\n')
        stream.write(f'Could not find file {filename}\n')
        stream.write('Are you sure you are running this program from the same directory\n')
        stream.write('that you ran the profiler from?\n')
        stream.write("Continuing without the function's contents.\n")
        # Fake empty lines so we can see the timings, if not the code.
        nlines = 1 if not linenos else max(linenos) - min(min(linenos), start_lineno) + 1
        sublines = [''] * nlines
    for lineno, nhits, time in timings:
        if total_time == 0:  # Happens rarely on empty function
            percent = ''
        else:
            percent = '%5.1f' % (100 * time / total_time)
        d[lineno] = (nhits,
                     '%5.1f' % (time * scalar),
                     '%5.1f' % (float(time) * scalar / nhits),
                     percent)
    linenos = range(start_lineno, start_lineno + len(sublines))
    empty = ('', '', '', '')
    header = template % ('Line #', 'Hits', 'Time', 'Per Hit', '% Time',
                         'Line Contents')
    stream.write('\n')
    stream.write(header)
    stream.write('\n')
    stream.write('=' * len(header))
    stream.write('\n')
    for lineno, line in zip(linenos, sublines):
        nhits, time, per_hit, percent = d.get(lineno, empty)
        txt = template % (lineno, nhits, time, per_hit, percent,
                          line.rstrip('\n').rstrip('\r'))
        stream.write(txt)
        stream.write('\n')
    stream.write('\n')


def show_text(stats, unit, output_unit=None, stream=None, stripzeros=False):
    """ Show text for the given timings.
    """
    if stream is None:
        stream = sys.stdout

    if output_unit is not None:
        stream.write('Timer unit: %g s\n\n' % output_unit)
    else:
        stream.write('Timer unit: %g s\n\n' % unit)

    for (fn, lineno, name), timings in sorted(stats.items()):
        show_func(fn, lineno, name, stats[fn, lineno, name], unit,
                  output_unit=output_unit, stream=stream,
                  stripzeros=stripzeros)


def load_stats(filename: List[str]) -> List[LineStats]:
    """ Utility function to load a pickled LineStats object from given
    filenames, which should be splitted by `,`.
    """
    filenames = [f for f in filenames.split(',') if len(f) > 0]
    results = []
    for filename in filenames:
        with open(filename, 'rb') as f:
            results.append(pickle.load(f))
    return results


def aggregate_stats(results: List[LineStats]) -> LineStats:
    """Aggregate result. 
    
    Currently assuming time units are same.
    """
    merged_result = {}
    for result in results:
        for k in result.timings:
            if k not in merged_result:
                merged_result[k] = result.timings[k]
            else:
                merged_result[k] = _merge_timings(result.timings[k], merged_result[k])
    return LineStats(merged_result, result.unit)


def _merge_timings(value1: List[Sequence[int]], value2: List[Sequence[int]]) -> List [Sequence [int]]:
    """Merge two result."""
    new value = []
    for v in value1:
        for _v in value2:
            if v[0] == _v[0]:
                new_v = (v[0], v[1] + _v[1], v[2] + _v[2])
                break
        else:
            new_v = v
        new_value.append(new_v)

    # some line may not in value1, therefore add it back
    line_nums = [v[0] for v in new_value]
    for v in value2:
        if v[0]not in line_nums:
            new_value.append(v)

    # sort the result according to line number
    new_value = sorted(new_value, key=lambda x: x[0])
    return new_value


def main():
    def positive_float(value):
        val = float(value)
        if val <= 0:
            raise ArgumentError
        return val

    parser = ArgumentParser()
    parser.add_argument('-V', '--version', action='version', version=__version__)
    parser.add_argument(
        '-u',
        '--unit',
        default='1e-6',
        type=positive_float,
        help='Output unit (in seconds) in which the timing info is displayed (default: 1e-6)',
    )
    parser.add_argument(
        '-z',
        '--skip-zero',
        action='store_true',
        help='Hide functions which have not been called',
    )
    parser.add_argument('profile_output', help='*.lprof file created by kernprof')

    args = parser.parse_args()
    lstats = load_stats(args.profile_output)
    lstats = aggregate_stats(lstats)
    show_text(lstats.timings, lstats.unit, output_unit=args.unit, stripzeros=args.skip_zero)


if __name__ == '__main__':
    main()
