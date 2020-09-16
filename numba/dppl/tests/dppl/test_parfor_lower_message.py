from __future__ import print_function, division, absolute_import

import numpy as np
import numba
from numba import dppl
from numba.dppl.testing import unittest
from numba.dppl.testing import DPPLTestCase
from numba import njit, prange
from numba.tests.support import captured_stdout
import dppl.ocldrv as ocldrv


def prange_example():
    n = 10
    a = np.ones((n, n), dtype=np.float64)
    b = np.ones((n, n), dtype=np.float64)
    c = np.ones((n, n), dtype=np.float64)
    for i in prange(n):
        a[i] = b[i] + c[i]


@unittest.skipUnless(ocldrv.has_gpu_device, 'test only on GPU system')
class TestParforMessage(DPPLTestCase):
    def test_parfor_message(self):
        numba.dppl.compiler.DEBUG = 1
        jitted = njit(parallel={'offload':True})(prange_example)
        
        with captured_stdout() as got:
            jitted()

        self.assertTrue('Parfor lowered on GPU' in got.getvalue())


if __name__ == '__main__':
    unittest.main()
