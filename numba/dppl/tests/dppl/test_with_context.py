import numba
import numpy as np
from numba import dppl, njit
from numba.core import errors
from numba.tests.support import captured_stdout
from numba.dppl.testing import DPPLTestCase, unittest
import dpctl
import dpctl.ocldrv as ocldrv


@unittest.skipIf(not dpctl.has_gpu_queues(), "No GPU platforms available")
@unittest.skipIf(not dpctl.has_cpu_queues(), "No CPU platforms available")
class TestWithDPPLContext(DPPLTestCase):
    def test_with_dppl_context_gpu(self):

        @njit
        def nested_func(a, b):
            np.sin(a, b)

        @njit
        def func(b):
            a = np.ones((64), dtype=np.float64)
            nested_func(a, b)

        numba.dppl.compiler.DEBUG = 1
        expected = np.ones((64), dtype=np.float64)
        got_gpu = np.ones((64), dtype=np.float64)

        with captured_stdout() as got_gpu_message:
            with dpctl.device_context(dpctl.device_type.gpu):
                func(got_gpu)

        func(expected)

        np.testing.assert_array_equal(expected, got_gpu)
        self.assertTrue('Parfor lowered on DPPL-device' in got_gpu_message.getvalue())


    def test_with_dppl_context_cpu(self):
        
        @njit
        def nested_func(a, b):
            np.sin(a, b)

        @njit
        def func(b):
            a = np.ones((64), dtype=np.float64)
            nested_func(a, b)

        numba.dppl.compiler.DEBUG = 1
        expected = np.ones((64), dtype=np.float64)
        got_cpu = np.ones((64), dtype=np.float64)

        with captured_stdout() as got_cpu_message:
            with dpctl.device_context(dpctl.device_type.cpu):
                func(got_cpu)

        func(expected)

        np.testing.assert_array_equal(expected, got_cpu)
        self.assertTrue('Parfor lowered on DPPL-device' not in got_cpu_message.getvalue())


    def test_with_dppl_context_target(self):

        @njit(target='cpu')
        def nested_func_target(a, b):
            np.sin(a, b)

        @njit(target='gpu')
        def func_target(b):
            a = np.ones((64), dtype=np.float64)
            nested_func_target(a, b)

        @njit
        def func_no_target(b):
            a = np.ones((64), dtype=np.float64)
            nested_func_target(a, b)

        a = np.ones((64), dtype=np.float64)
        b = np.ones((64), dtype=np.float64)

        with self.assertRaises(errors.UnsupportedError) as raises_1:
            with dpctl.device_context(dpctl.device_type.gpu):
                nested_func_target(a, b)

        with self.assertRaises(errors.UnsupportedError) as raises_2:
            with dpctl.device_context(dpctl.device_type.gpu):
                func_target(a)

        with self.assertRaises(errors.UnsupportedError) as raises_3:
            with dpctl.device_context(dpctl.device_type.gpu):
                func_no_target(a)

        msg = "Unsupported defined 'target' with using context device"
        self.assertTrue(msg in str(raises_1.exception))
        self.assertTrue(msg in str(raises_2.exception))
        self.assertTrue(msg in str(raises_3.exception))


if __name__ == '__main__':
    unittest.main()
