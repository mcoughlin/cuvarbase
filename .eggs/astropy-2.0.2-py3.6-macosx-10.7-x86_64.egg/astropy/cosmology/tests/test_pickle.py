# Licensed under a 3-clause BSD style license - see LICENSE.rst
from __future__ import absolute_import, division, print_function, unicode_literals

import pytest

from ...tests.helper import pickle_protocol, check_pickling_recovery
from ...extern.six.moves import zip
from ... import cosmology as cosm

originals = [cosm.FLRW]
xfails = [False]


@pytest.mark.parametrize(("original", "xfail"),
                         zip(originals, xfails))
def test_flrw(pickle_protocol, original, xfail):
    if xfail:
        pytest.xfail()
    check_pickling_recovery(original, pickle_protocol)
