from __future__ import division

from contextlib import contextmanager

import numpy as np

from numba import ocl, config
from numba.ocl.testing import unittest
from numba.tests.support import captured_stderr


class TestDeallocation(unittest.TestCase):
    def test_max_pending_count(self):
        # get deallocation manager and flush it
        deallocs = ocl.current_context().deallocations
        deallocs.clear()
        self.assertEqual(len(deallocs), 0)
        # deallocate to maximum count
        for i in range(config.OCL_DEALLOCS_COUNT):
            ocl.to_device(np.arange(1))
            self.assertEqual(len(deallocs), i + 1)
        # one more to trigger .clear()
        ocl.to_device(np.arange(1))
        self.assertEqual(len(deallocs), 0)

    def test_max_pending_bytes(self):
        # get deallocation manager and flush it
        ctx = ocl.current_context()
        deallocs = ctx.deallocations
        deallocs.clear()
        self.assertEqual(len(deallocs), 0)

        mi = ctx.get_memory_info()

        max_pending = 10**6  # 1MB
        old_ratio = config.OCL_DEALLOCS_RATIO
        try:
            # change to a smaller ratio
            config.OCL_DEALLOCS_RATIO = max_pending / mi.total
            self.assertEqual(deallocs._max_pending_bytes, max_pending)

            # deallocate half the max size
            ocl.to_device(np.ones(max_pending // 2, dtype=np.int8))
            self.assertEqual(len(deallocs), 1)

            # deallocate another remaining
            ocl.to_device(np.ones(max_pending - deallocs._size, dtype=np.int8))
            self.assertEqual(len(deallocs), 2)

            # another byte to trigger .clear()
            ocl.to_device(np.ones(1, dtype=np.int8))
            self.assertEqual(len(deallocs), 0)
        finally:
            # restore old ratio
            config.OCL_DEALLOCS_RATIO = old_ratio


class TestDeferCleanup(unittest.TestCase):
    def test_basic(self):
        harr = np.arange(5)
        darr1 = ocl.to_device(harr)
        deallocs = ocl.current_context().deallocations
        deallocs.clear()
        self.assertEqual(len(deallocs), 0)
        with ocl.defer_cleanup():
            darr2 = ocl.to_device(harr)
            del darr1
            self.assertEqual(len(deallocs), 1)
            del darr2
            self.assertEqual(len(deallocs), 2)
            deallocs.clear()
            self.assertEqual(len(deallocs), 2)

        deallocs.clear()
        self.assertEqual(len(deallocs), 0)

    def test_nested(self):
        harr = np.arange(5)
        darr1 = ocl.to_device(harr)
        deallocs = ocl.current_context().deallocations
        deallocs.clear()
        self.assertEqual(len(deallocs), 0)
        with ocl.defer_cleanup():
            with ocl.defer_cleanup():
                darr2 = ocl.to_device(harr)
                del darr1
                self.assertEqual(len(deallocs), 1)
                del darr2
                self.assertEqual(len(deallocs), 2)
                deallocs.clear()
                self.assertEqual(len(deallocs), 2)
            deallocs.clear()
            self.assertEqual(len(deallocs), 2)

        deallocs.clear()
        self.assertEqual(len(deallocs), 0)

    def test_exception(self):
        harr = np.arange(5)
        darr1 = ocl.to_device(harr)
        deallocs = ocl.current_context().deallocations
        deallocs.clear()
        self.assertEqual(len(deallocs), 0)

        class CustomError(Exception):
            pass

        with self.assertRaises(CustomError):
            with ocl.defer_cleanup():
                darr2 = ocl.to_device(harr)
                del darr2
                self.assertEqual(len(deallocs), 1)
                deallocs.clear()
                self.assertEqual(len(deallocs), 1)
                raise CustomError
        deallocs.clear()
        self.assertEqual(len(deallocs), 0)
        del darr1
        self.assertEqual(len(deallocs), 1)
        deallocs.clear()
        self.assertEqual(len(deallocs), 0)


class TestDeferCleanupAvail(unittest.TestCase):
    def test_context_manager(self):
        # just make sure the API is available
        with ocl.defer_cleanup():
            pass


class TestDel(unittest.TestCase):
    """
    Ensure resources are deleted properly without ignored exception.
    """
    @contextmanager
    def check_ignored_exception(self, ctx):
        with captured_stderr() as cap:
            yield
            ctx.deallocations.clear()
        self.assertFalse(cap.getvalue())

    def test_stream(self):
        ctx = ocl.current_context()
        stream = ctx.create_stream()
        with self.check_ignored_exception(ctx):
            del stream

    def test_event(self):
        ctx = ocl.current_context()
        event = ctx.create_event()
        with self.check_ignored_exception(ctx):
            del event

    def test_pinned_memory(self):
        ctx = ocl.current_context()
        mem = ctx.memhostalloc(32)
        with self.check_ignored_exception(ctx):
            del mem

    def test_mapped_memory(self):
        ctx = ocl.current_context()
        mem = ctx.memhostalloc(32, mapped=True)
        with self.check_ignored_exception(ctx):
            del mem

    def test_device_memory(self):
        ctx = ocl.current_context()
        mem = ctx.memalloc(32)
        with self.check_ignored_exception(ctx):
            del mem


if __name__ == '__main__':
    unittest.main()

