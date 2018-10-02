# -*- coding: utf-8 -*-

# Simple Gaussian ModelLink
# Compatible with Python 2.7 and 3.5+

"""
GaussianLink
============
Provides the definition of the :class:`~GaussianLink` class.

"""


# %% IMPORTS
# Future imports
from __future__ import absolute_import, division, print_function

# Package imports
import numpy as np

# PRISM imports
from .._internal import check_val
from .modellink import ModelLink

# All declaration
__all__ = ['GaussianLink']


# %% CLASS DEFINITION
class GaussianLink(ModelLink):
    """
    :class:`~ModelLink` class wrapper for a simple Gaussian model, used for
    testing the functionality of the *PRISM* pipeline in unittests.

    Formatting data_idx
    -------------------
    x : int
        The value that needs to be used for :math:`x` in the function
        :math:`\\sum_i A_i\\exp\\left(-\\frac{(x-B_i)^2}{2C_i^2}\\right)` to
        obtain the data value.

    """

    def __init__(self, n_gaussians=1, *args, **kwargs):
        """
        Initialize an instance of the :class:`~GaussianLink` class.

        Optional
        --------
        n_gaussians : int. Default: 1
            The number of Gaussians to use for the Gaussian model in this
            instance. The resulting number of model parameters :attr:`~n_par`
            will be :math:`3*n_{gaussians}`.

        """

        # Set the number of Gaussians
        self._n_gaussians = check_val(n_gaussians, 'n_gaussians', 'pos', 'int')

        # Set the name of this GaussianLink instance
        self.name = 'GaussianLink_n%i' % (self._n_gaussians)

        # Request single model calls
        self.multi_call = False

        # Request only controller calls
        self.MPI_call = False

        # Inheriting ModelLink __init__()
        super(GaussianLink, self).__init__(*args, **kwargs)

    @property
    def _default_model_parameters(self):
        # Set default parameters for every Gaussian
        A = [1, 10, 5]
        B = [0, 10, 5]
        C = [0, 5, 2]

        # Create default parameters dict and return it
        par_dict = {}
        for i in range(1, self._n_gaussians+1):
            par_dict['A%i' % (i)] = list(A)
            par_dict['B%i' % (i)] = list(B)
            par_dict['C%i' % (i)] = list(C)
        return(par_dict)

    def call_model(self, emul_i, model_parameters, data_idx):
        par = model_parameters
        mod_set = 0
        for i in range(1, self._n_gaussians+1):
            mod_set +=\
                par['A%i' % (i)]*np.exp(-1*((data_idx-par['B%i' % (i)])**2 /
                                            (2*par['C%i' % (i)]**2)))

        return(mod_set)

    def get_md_var(self, emul_i, data_idx):
        return(pow(0.1*np.ones(len(data_idx)), 2))