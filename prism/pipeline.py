# -*- coding: utf-8 -*-

"""
Pipeline
========
Provides the definition of the main class of the PRISM package, the
:class:`~Pipeline` class.


Available classes
-----------------
:class:`~Pipeline`
    Defines the :class:`~Pipeline` class of the PRISM package.

"""


# %% IMPORTS
# Future imports
from __future__ import (absolute_import, division, print_function,
                        with_statement)

# Built-in imports
import logging
import os
from os import path
import sys
from time import strftime, strptime, time
import warnings

# Package imports
from e13tools import InputError, ShapeError
from e13tools.math import nCr
from e13tools.sampling import lhd
from mpi4py import MPI
import numpy as np
from numpy.random import normal, random
from sortedcontainers import SortedSet

# PRISM imports
from ._docstrings import call_emul_i_doc, std_emul_i_doc, user_emul_i_doc
from ._internal import (PRISM_File, RequestError, check_bool, check_float,
                        check_nneg_float, check_pos_int, convert_str_seq,
                        docstring_copy, docstring_substitute, move_logger,
                        start_logger)
from .emulator import Emulator
from .projection import Projection

# All declaration
__all__ = ['Pipeline']

# Python2/Python3 compatibility
if(sys.version_info.major >= 3):
    unicode = str


# %% PIPELINE CLASS DEFINITION
# OPTIMIZE: Rewrite PRISM into MPI?
# TODO: Allow user to switch between emulation and modelling
# TODO: Implement multivariate implausibilities
# OPTIMIZE: Overlap plausible regions to remove boundary artifacts?
# TODO: Allow ModelLink to provide full data set, Pipeline selects data itself?
# TODO: Think of a way to allow no ModelLink instance to be provided.
# This could be done with a DummyLink, but md_var is then uncallable.
class Pipeline(object):
    """
    Defines the :class:`~Pipeline` class of the PRISM package.

    """

    # TODO: Should prism_file be defaulted to None?
    def __init__(self, modellink, root_dir=None, working_dir=None,
                 prefix='prism_', hdf5_file='prism.hdf5',
                 prism_file='prism.txt', emul_type='default'):
        """
        Initialize an instance of the :class:`~Pipeline` class.

        Parameters
        ----------
        modellink : :obj:`~ModelLink` object
            Instance of the :class:`~ModelLink` class that links the emulated
            model to this :obj:`~Pipeline` object.

        Optional
        --------
        root_dir : str or None. Default: None
            String containing the absolute path of the root directory where all
            working directories are stored. If *None*, root directory will be
            set to the directory this class was initialized at.
        working_dir : str, int or None. Default: None
            String containing the name of the working directory of the emulator
            in `root_dir`. If int, a new working directory will be created in
            `root_dir`. If *None*, working directory is set to the last one
            that was created in `root_dir` that starts with the given `prefix`.
            If no directories are found, one will be created.
        prefix : str. Default: 'prism_'
            String containing a prefix that is used for naming new working
            directories or scan for existing ones.
        hdf5_file : str. Default: 'prism.hdf5'
            String containing the name of the HDF5-file in `working_dir` to be
            used in this class instance. Different types of HDF5-files can be
            provided:
                *Non-existing HDF5-file*: This file will be created and used to
                save the constructed emulator system in.

                *Existing HDF5-file*: This file will be used to regenerate a
                previously constructed emulator system.
        prism_file : str or None. Default: 'prism.txt'
            String containing the absolute or relative path to the TXT-file
            containing the PRISM parameters that need to be changed from their
            default values. If a relative path is given, its path must be
            relative to `root_dir` or the current directory. If *None*, no
            changes will be made to the default parameters.

        """

        # Determine MPI ranks, size and statuses
        self._rank = MPI.COMM_WORLD.Get_rank()
        self._size = MPI.COMM_WORLD.Get_size()
        self._is_controller = 0
        self._is_worker = 0
        if(self._rank == 0):
            self._is_controller = 1
        else:
            self._is_worker = 1

        # Controller only
        if self._is_controller:
            # Start logging
            logging_file = start_logger()
            logger = logging.getLogger('PIPELINE')
            logger.info("")

            # Initialize class
            logger = logging.getLogger('INIT')
            logger.info("Initializing Pipeline class.")

            # Obtain paths
            self._get_paths(root_dir, working_dir, prefix, hdf5_file,
                            prism_file)

            # Move logger to working directory
            move_logger(self._working_dir, logging_file)

            # Initialize Emulator class
            if(emul_type == 'default'):
                self._emulator = Emulator(self, modellink)
            else:
                raise RequestError("Input argument 'emul_type' is invalid!")

        # Remaining workers
        else:
            # Listen for controller sending updated modellink object
            self._modellink = MPI.COMM_WORLD.recv(source=0, tag=888+self._rank)

        # Let controller read in the data
        if self._is_controller:
            # Read/load in pipeline parameters
            self._read_parameters()
            self._load_data()

        # Print out the details of the current state of the pipeline
        self.details()

    # Allows one to call one full loop of the PRISM pipeline
    @docstring_substitute(emul_i=call_emul_i_doc)
    def __call__(self, emul_i=None):
        """
        Calls the :meth:`~construct` method to start the construction of the
        given iteration of the emulator system and creates the projection
        figures right afterward if this construction was successful.

        Optional
        --------
        %(emul_i)s

        """

        # Perform construction
        try:
            self.construct(emul_i)
        except Exception:
            raise
        else:
            try:
                # Perform projection
                self.project()

                # Print details
                self.details()
            except Exception:
                raise


# %% CLASS PROPERTIES
    # TODO: Hide class attributes that do not exist yet
    # MPI properties
    @property
    def rank(self):
        """
        The rank of this MPI process in MPI.COMM_WORLD.

        """

        return(self._rank)

    @property
    def size(self):
        """
        The number of MPI processes in MPI.COMM_WORLD.

        """

        return(self._size)

    @property
    def is_controller(self):
        """
        Bool indicating whether or not this MPI process is a controller
        process.

        """

        return(bool(self._is_controller))

    @property
    def is_worker(self):
        """
        Bool indicating whether or not this MPI process is a worker process.

        """

        return(bool(self._is_worker))

    # Pipeline Settings/Attributes/Details
    @property
    def root_dir(self):
        """
        Absolute path to the root directory.

        """

        return(self._root_dir)

    @property
    def working_dir(self):
        """
        Absolute path to the working directory.

        """

        return(self._working_dir)

    @property
    def prefix(self):
        """
        String used as a prefix when naming new working directories.

        """

        return(self._prefix)

    @property
    def hdf5_file(self):
        """
        Absolute path to the loaded HDF5-file.

        """

        return(self._hdf5_file)

    @property
    def hdf5_file_name(self):
        """
        Name of loaded HDF5-file.

        """

        return(self._hdf5_file_name)

    @property
    def prism_file(self):
        """
        Absolute path to PRISM parameters file.

        """

        return(self._prism_file)

    @property
    def modellink(self):
        """
        The :obj:`~ModelLink` instance provided during Pipeline initialization.

        """

        return(self._modellink)

    @property
    def emulator(self):
        """
        The :obj:`~Emulator` instance created during Pipeline initialization.

        """

        return(self._emulator)

    @property
    def criterion(self):
        """
        String or float indicating which criterion to use in the
        :func:`e13tools.sampling.lhd` function.

        """

        return(self._criterion)

    @property
    def do_active_anal(self):
        """
        Bool indicating whether or not to do an active parameters analysis.

        """

        return(bool(self._do_active_anal))

    @property
    def freeze_active_par(self):
        """
        Bool indicating whether or not previously active parameters always stay
        active.

        """

        return(bool(self._freeze_active_par))

    @property
    def pot_active_par(self):
        """
        List of potentially active parameters. Only parameters from this list
        can become active.

        """

        return([self._modellink._par_name[i] for i in self._pot_active_par])

    @property
    def n_sam_init(self):
        """
        Number of evaluation samples used to construct the first iteration of
        the emulator system.

        """

        return(self._n_sam_init)

    @property
    def n_eval_sam(self):
        """
        List containing the number of evaluation samples used to analyze the
        corresponding emulator iteration of the emulator system. The number of
        plausible evaluation samples is stored in :attr:`~Pipeline.n_impl_sam`.

        """

        return(self._n_eval_sam)

    @property
    def base_eval_sam(self):
        """
        Base number of emulator evaluations used to analyze the emulator
        system. This number is scaled up by the number of model parameters and
        the current emulator iteration to generate the true number of emulator
        evaluations (:attr:`~Pipeline.n_eval_sam`).

        """

        return(self._base_eval_sam)

    @property
    def impl_cut(self):
        """
        List of lists containing all univariate implausibility cut-offs. A zero
        indicates a wildcard.

        """

        return(self._impl_cut)

    @property
    def cut_idx(self):
        """
        List of list indices of the first non-wildcard cut-off in impl_cut.

        """

        return(self._cut_idx)

    @property
    def prc(self):
        """
        Bool indicating whether or not plausible regions have been found in the
        last emulator iteration.

        """

        return(bool(self._prc))

    @property
    def n_impl_sam(self):
        """
        List of number of model evaluation samples that have been added to the
        corresponding emulator iteration.

        """

        return(self._n_impl_sam)

    @property
    def impl_sam(self):
        """
        Array containing all model evaluation samples that will be added to the
        next emulator iteration.

        """

        return(self._impl_sam)


# %% GENERAL CLASS METHODS
    # Function containing the model output for a given set of parameter values
    # TODO: May want to save all model output immediately to prevent data loss
    @docstring_substitute(emul_i=std_emul_i_doc)
    def _call_model(self, emul_i, par_set):
        """
        Obtain the output that is generated by the model for a given model
        parameter value set `par_set`. The current emulator iteration `emul_i`
        is also provided in case it is required by the :class:`~ModelLink`
        subclass.

        Parameters
        ----------
        %(emul_i)s
        par_set : 1D array_like
            Model parameter value set to calculate the model output for.

        Returns
        -------
        mod_out : 1D :obj:`~numpy.ndarray` object
            Model output corresponding to given `par_set`.

        """

        # Make sure par_set is at least 1D and a numpy array
        sam = np.array(par_set, ndmin=1)

        # Log that model is being called
        if self._is_controller:
            logger = logging.getLogger('CALL_MODEL')
            logger.info("Calling model at parameters %s." % (sam))

        # Create par_dict
        par_dict = dict(zip(self._modellink._par_name, sam))

        # Obtain model output
        mod_out = self._modellink.call_model(emul_i, par_dict,
                                             self._modellink._data_idx)

        # Log that calling model has been finished
        if self._is_controller:
            logger.info("Model returned %s." % (mod_out))

        # Return it
        return(np.array(mod_out))

    # Function containing the model output for a given set of parameter samples
    @docstring_substitute(emul_i=std_emul_i_doc)
    def _multi_call_model(self, emul_i, sam_set):
        """
        Obtain the output set that is generated by the model for a given set of
        model parameter samples `sam_set`. The current emulator iteration
        `emul_i` is also provided in case it is required by the
        :class:`~ModelLink` subclass.

        This is a multi-version of the :meth:`~Pipeline._call_model` method.

        Parameters
        ----------
        %(emul_i)s
        sam_set : 2D array_like
            Model parameter sample set to calculate the model output for.

        Returns
        -------
        mod_set : 2D :obj:`~numpy.ndarray` object
            Model output set corresponding to given `sam_set`.

        """

        # Make sure that sam_set is at least 2D and a numpy array
        sam_set = np.array(sam_set, ndmin=2)

        # Log that model is being multi-called
        if self._is_controller:
            logger = logging.getLogger('CALL_MODEL')
            logger.info("Multi-calling model for sample set of size %s."
                        % (np.shape(sam_set)[0]))

        # Create sam_dict
        sam_dict = dict(zip(self._modellink._par_name, sam_set.T))

        # Obtain set of model outputs
        mod_set = self._modellink.call_model(emul_i, sam_dict,
                                             self._modellink._data_idx)

        # Log that multi-calling model has been finished
        if self._is_controller:
            logger.info("Finished model multi-call.")

        # Return it
        return(np.array(mod_set).T)

    # This function automatically loads default pipeline parameters
    def _get_default_parameters(self):
        """
        Generates a dict containing default values for all pipeline parameters.

        Returns
        -------
        par_dict : dict
            Dict containing all default pipeline parameter values.

        """

        # Log this
        logger = logging.getLogger('INIT')
        logger.info("Generating default pipeline parameter dict.")

        # Create parameter dict with default parameters
        par_dict = {'n_sam_init': '500',
                    'base_eval_sam': '800',
                    'impl_cut': '[0, 4.0, 3.8, 3.5]',
                    'criterion': "'multi'",
                    'do_active_anal': 'True',
                    'freeze_active_par': 'True',
                    'pot_active_par': 'None'}

        # Log end
        logger.info("Finished generating default pipeline parameter dict.")

        # Return it
        return(par_dict)

    # Read in the parameters from the provided parameter file
    def _read_parameters(self):
        """
        Reads in the pipeline parameters from the provided PRISM parameter file
        saves them in the current :obj:`~Pipeline` instance.

        """

        # Log that the PRISM parameter file is being read
        logger = logging.getLogger('INIT')
        logger.info("Reading pipeline parameters.")

        # Obtaining default pipeline parameter dict
        par_dict = self._get_default_parameters()

        # Read in data from provided PRISM parameters file
        if self._prism_file is not None:
            pipe_par = np.genfromtxt(self._prism_file, dtype=(str),
                                     delimiter=':', autostrip=True)

            # Make sure that pipe_par is 2D
            pipe_par = np.array(pipe_par, ndmin=2)

            # Combine default parameters with read-in parameters
            par_dict.update(pipe_par)

        # More logging
        logger.info("Checking compatibility of provided pipeline parameters.")

        # GENERAL
        # Number of starting samples
        self._n_sam_init = check_pos_int(int(par_dict['n_sam_init']),
                                         'n_sam_init')

        # Base number of emulator evaluation samples
        self._base_eval_sam = check_pos_int(int(par_dict['base_eval_sam']),
                                            'base_eval_sam')

        # Criterion parameter used for Latin Hypercube Sampling
        if(par_dict['criterion'].lower() == 'none'):
            self._criterion = None
        elif par_dict['criterion'].lower() in ('false', 'true'):
            logger.error("Input argument 'criterion' does not accept values "
                         "of type 'bool'!")
            raise TypeError("Input argument 'criterion' does not accept "
                            "values of type 'bool'!")
        else:
            try:
                self._criterion = float(par_dict['criterion'])
            except ValueError:
                self._criterion = str(par_dict['criterion']).replace("'", '')

        # Obtain the bool determining whether to do an active parameters
        # analysis
        self._do_active_anal = check_bool(par_dict['do_active_anal'],
                                          'do_active_anal')

        # Obtain the bool determining whether active parameters stay active
        self._freeze_active_par = check_bool(par_dict['freeze_active_par'],
                                             'freeze_active_par')

        # Check which parameters can potentially be active
        if(par_dict['pot_active_par'].lower() == 'none'):
            self._pot_active_par = np.array(range(self._modellink._n_par))
        elif par_dict['pot_active_par'].lower() in ('false', 'true'):
            logger.error("Input argument 'pot_active_par' does not accept "
                         "values of type 'bool'!")
            raise TypeError("Input argument 'pot_active_par' does not accept "
                            "values of type 'bool'!")
        else:
            # Remove all unwanted characters from the string and split it up
            pot_active_par = convert_str_seq(par_dict['pot_active_par'])

            # Check elements if they are ints or strings, and if they are valid
            for i, string in enumerate(pot_active_par):
                try:
                    try:
                        par_idx = int(string)
                    except ValueError:
                        pot_active_par[i] =\
                            self._modellink._par_name.index(string)
                    else:
                        self._modellink._par_name[par_idx]
                        pot_active_par[i] = par_idx % self._modellink._n_par
                except Exception as error:
                    logger.error("Input argument 'pot_active_par' is invalid! "
                                 "(%s)" % (error))
                    raise InputError("Input argument 'pot_active_par' is "
                                     "invalid! (%s)" % (error))

            # If everything went without exceptions, check if list is not empty
            if(len(pot_active_par) != 0):
                self._pot_active_par =\
                    np.array(list(SortedSet(pot_active_par)))
            else:
                logger.error("Input argument 'pot_active_par' is empty!")
                raise ValueError("Input argument 'pot_active_par' is empty!")

        # Log that reading has been finished
        logger.info("Finished reading pipeline parameters.")

    # This function controls how n_eval_samples is calculated
    @docstring_substitute(emul_i=std_emul_i_doc)
    def _get_n_eval_sam(self, emul_i):
        """
        This function calculates the total amount of emulator evaluation
        samples at a given emulator iteration `emul_i` from the
        `base_eval_sam` provided during class initialization.

        Parameters
        ----------
        %(emul_i)s

        Returns
        -------
        n_eval_sam : int
            Number of emulator evaluation samples.

        """

        # Calculate n_eval_sam
        return(emul_i*self._base_eval_sam*self._modellink._n_par)

    # Obtains the paths for the root directory, working directory, pipeline
    # hdf5-file and prism parameters file
    def _get_paths(self, root_dir, working_dir, prefix, hdf5_file, prism_file):
        """
        Obtains the path for the root directory, working directory, HDF5-file
        and parameters file for PRISM.

        Parameters
        ----------
        root_dir : str or None
            String containing the absolute path to the root directory where all
            working directories are stored. If *None*, root directory will be
            set to the directory where this class was initialized at.
        working_dir : str or None
            String containing the name of the working directory of the emulator
            in `root_dir`. If int, a new working directory will be created in
            `root_dir`. If *None*, working directory is set to the last one
            that was created in `root_dir` that starts with the given `prefix`.
            If no directories are found, one will be created.
        prefix : str
            String containing a prefix that is used for naming new working
            directories or scan for existing ones.
        hdf5_file : str
            String containing the name of the HDF5-file in `working_dir` to be
            used in this class instance.
        prism_file : str or None
            String containing the absolute or relative path to the TXT-file
            containing the PRISM parameters that need to be changed from their
            default values. If a relative path is given, its path must be
            relative to `root_dir` or the current directory. If *None*, no
            changes will be made to the default parameters.

        Generates
        ---------
        The absolute paths to the root directory, working directory, pipeline
        HDF5-file and PRISM parameters file.

        """

        # Set logging system
        logger = logging.getLogger('INIT')
        logger.info("Obtaining related directory and file paths.")

        # Obtain root directory path
        # If one did not specify a root directory, set it to default
        if root_dir is None:
            logger.info("No root directory specified, setting it to default.")
            self._root_dir = path.abspath('.')
            logger.info("Root directory set to '%s'." % (self._root_dir))

        # If one specified a root directory, use it
        elif isinstance(root_dir, (str, unicode)):
            logger.info("Root directory specified.")
            self._root_dir = path.abspath(root_dir)
            logger.info("Root directory set to '%s'." % (self._root_dir))

            # Check if this directory already exists
            try:
                logger.info("Checking if root directory already exists.")
                os.mkdir(self._root_dir)
            except OSError:
                logger.info("Root directory already exists.")
                pass
            else:
                logger.info("Root directory did not exist, created it.")
                pass
        else:
            logger.error("Input argument 'root_dir' is invalid!")
            raise InputError("Input argument 'root_dir' is invalid!")

        # Check if a valid working directory prefix string is given
        if isinstance(prefix, (str, unicode)):
            self._prefix = prefix
            prefix_len = len(prefix)
        else:
            logger.error("Input argument 'prefix' is not of type 'str'!")
            raise TypeError("Input argument 'prefix' is not of type 'str'!")

        # Obtain working directory path
        # If one did not specify a working directory, obtain it
        if working_dir is None:
            logger.info("No working directory specified, trying to load last "
                        "one created.")
            dirnames = next(os.walk(self._root_dir))[1]
            emul_dirs = list(dirnames)

            # Check which directories in the root_dir satisfy the default
            # naming scheme of the emulator directories
            for dirname in dirnames:
                if(dirname[0:prefix_len] != self._prefix):
                    emul_dirs.remove(dirname)
                else:
                    try:
                        strptime(dirname[prefix_len:prefix_len+10], '%Y-%m-%d')
                    except ValueError:
                        emul_dirs.remove(dirname)

            # If no working directory exists, make a new one
            if(len(emul_dirs) == 0):
                logger.info("No working directories found, creating it.")
                working_dir = ''.join([self._prefix, strftime('%Y-%m-%d')])
                self._working_dir = path.join(self._root_dir, working_dir)
                os.mkdir(self._working_dir)
                logger.info("Working directory set to '%s'." % (working_dir))

            # If working directories exist, load last one created
            else:
                logger.info("Working directories found, loading last one.")
                emul_dirs.sort(reverse=True)
                working_dir = emul_dirs[0]
                self._working_dir = path.join(self._root_dir, working_dir)
                logger.info("Working directory set to '%s'." % (working_dir))

        # If one requested a new working directory
        elif isinstance(working_dir, int):
            logger.info("New working directory requested, creating it.")
            working_dir = ''.join([self._prefix, strftime('%Y-%m-%d')])
            dirnames = next(os.walk(self._root_dir))[1]
            emul_dirs = list(dirnames)

            for dirname in dirnames:
                if(dirname[0:prefix_len+10] != working_dir):
                    emul_dirs.remove(dirname)

            # Check if other working directories already exist with the same
            # prefix and append a number to the name if this is the case
            emul_dirs.sort(reverse=True)
            if(len(emul_dirs) == 0):
                pass
            elif(len(emul_dirs) == 1):
                working_dir = ''.join([working_dir, '_1'])
            else:
                working_dir =\
                    ''.join([working_dir, '_%s'
                             % (int(emul_dirs[0][prefix_len+11:])+1)])

            self._working_dir = path.join(self._root_dir, working_dir)
            os.mkdir(self._working_dir)
            logger.info("Working directory set to '%s'." % (working_dir))

        # If one specified a working directory, use it
        elif isinstance(working_dir, (str, unicode)):
            logger.info("Working directory specified.")
            self._working_dir =\
                path.join(self._root_dir, working_dir)
            logger.info("Working directory set to '%s'." % (working_dir))

            # Check if this directory already exists
            try:
                logger.info("Checking if working directory already exists.")
                os.mkdir(self._working_dir)
            except OSError:
                logger.info("Working directory already exists.")
                pass
            else:
                logger.info("Working directory did not exist, created it.")
                pass
        else:
            logger.error("Input argument 'working_dir' is invalid!")
            raise InputError("Input argument 'working_dir' is invalid!")

        # Obtain hdf5-file path
        if isinstance(hdf5_file, (str, unicode)):
            # Save hdf5-file path and name
            self._hdf5_file = path.join(self._working_dir, hdf5_file)
            logger.info("HDF5-file set to '%s'." % (hdf5_file))
            self._hdf5_file_name = path.join(working_dir, hdf5_file)

            # Save hdf5-file path as a PRISM_File class attribute
            PRISM_File._hdf5_file = self._hdf5_file
        else:
            logger.error("Input argument 'hdf5_file' is not of type 'str'!")
            raise TypeError("Input argument 'hdf5_file' is not of type 'str'!")

        # Obtain PRISM parameter file path
        # If no PRISM parameter file was provided
        if prism_file is None:
            self._prism_file = None

        # If a PRISM parameter file was provided
        elif isinstance(prism_file, (str, unicode)):
            if path.exists(prism_file):
                self._prism_file = path.abspath(prism_file)
            elif path.exists(path.join(self._root_dir, prism_file)):
                self._prism_file = path.join(self._root_dir, prism_file)
            else:
                logger.error("Input argument 'prism_file' is a non-existing "
                             "path (%s)!" % (prism_file))
                raise OSError("Input argument 'prism_file' is a non-existing "
                              "path (%s)!" % (prism_file))
            logger.info("PRISM parameters file set to '%s'." % (prism_file))
        else:
            logger.error("Input argument 'prism_file' is invalid!")
            raise InputError("Input argument 'prism_file' is invalid!")

    # This function generates mock data and loads it into ModelLink
    def _get_mock_data(self):
        """
        Generates mock data and loads it into the :obj:`~ModelLink` object that
        was provided during class initialization.
        This function overwrites the :class:`~ModelLink` properties holding the
        parameter estimates, data values and data errors.

        Generates
        ---------
        Overwrites the corresponding :class:`~ModelLink` class properties with
        the generated values.

        """

        # Controller only
        if self._is_controller:
            # Start logger
            logger = logging.getLogger('MOCK_DATA')

            # Log new mock_data being created
            logger.info("Generating mock data for new emulator system.")

            # Set non-default parameter estimate
            self._modellink._par_est =\
                (self._modellink._par_rng[:, 0] +
                 random(self._modellink._n_par) *
                 (self._modellink._par_rng[:, 1] -
                  self._modellink._par_rng[:, 0])).tolist()

        # MPI Barrier
        MPI.COMM_WORLD.Barrier()

        # Set non-default model data values
        if(self._modellink._MPI_call or
           (not self._modellink._MPI_call and self._is_controller)):
            if self._modellink._multi_call:
                # Multi-call model
                mod_out = self._multi_call_model(0, self._modellink._par_est)

                # Only controller receives output and can thus use indexing
                if self._is_controller:
                    self._modellink._data_val = mod_out[:, 0].tolist()

            else:
                # Controller only call model
                self._modellink._data_val =\
                    self._call_model(0, self._modellink._par_est).tolist()

        # Controller only
        if self._is_controller:
            # Use model discrepancy variance as model data errors
            try:
                md_var =\
                    self._modellink.get_md_var(0, self._modellink._data_idx)
            except NotImplementedError:
                md_var = pow(np.array(self._modellink._data_val)/6, 2)
            finally:
                # Check if all values are non-negative floats
                for value in md_var:
                    check_nneg_float(value, 'md_var')
                self._modellink._data_err = np.sqrt(md_var).tolist()

            # Add model data errors as noise to model data values
            self._modellink._data_val =\
                (self._modellink._data_val +
                 normal(scale=self._modellink._data_err)).tolist()

            # Logger
            logger.info("Generated mock data.")

        # Broadcast modellink object
        # TODO: Should entire modellink be broadcasted or just the changes?
        self._modellink = MPI.COMM_WORLD.bcast(self._modellink, 0)

    # This function loads pipeline data
    def _load_data(self):
        """
        Loads in all the important pipeline data into memory.
        If it is detected that the last emulator iteration has not been
        analyzed yet, the implausibility analysis parameters are read in from
        the PRISM parameters file and temporarily stored in memory.

        Generates
        ---------
        All relevant pipeline data is loaded into memory.

        """

        # Set the logger
        logger = logging.getLogger('LOAD_DATA')

        # Initialize all data sets with empty lists
        logger.info("Initializing pipeline data sets.")
        self._n_impl_sam = [[]]
        self._impl_cut = [[]]
        self._cut_idx = [[]]
        self._n_eval_sam = [[]]

        # If an emulator system currently exists, load in all data
        if self._emulator._emul_i:
            # Open hdf5-file
            with PRISM_File('r') as file:
                # Read in the data up to the last emulator iteration
                for i in range(1, self._emulator._emul_i+1):
                    # Get this emulator
                    emul = file['%s' % (i)]

                    # Check if analysis has been carried out (only if i=emul_i)
                    try:
                        self._impl_cut.append(emul.attrs['impl_cut'])

                    # If not, no plausible regions were found
                    except KeyError:
                        self._get_impl_par(True)

                    # If so, load in all data
                    else:
                        self._cut_idx.append(emul.attrs['cut_idx'])
                    finally:
                        self._n_impl_sam.append(emul.attrs['n_impl_sam'])
                        self._n_eval_sam.append(emul.attrs['n_eval_sam'])

                # Read in the samples that survived the implausibility check
                self._prc = int(emul.attrs['prc'])
                self._impl_sam = emul['impl_sam'][()]

    # This function saves pipeline data to hdf5
    def _save_data(self, data_dict):
        """
        Saves a given data dict {`keyword`: `data`} at the last emulator
        iteration to the HDF5-file and as an data attribute to the current
        :obj:`~Pipeline` instance.

        Parameters
        ----------
        data_dict : dict
            Dict containing the data that needs to be saved to the HDF5-file.

        Dict Variables
        --------------
        keyword : {'impl_cut', 'impl_sam', 'n_eval_sam'}
            String specifying the type of data that needs to be saved.
        data : int, float, list
            The actual data that needs to be saved at data keyword `keyword`.

        Generates
        ---------
        The specified data is saved to the HDF5-file.

        """

        # Do some logging
        logger = logging.getLogger('SAVE_DATA')

        # Obtain last emul_i
        emul_i = self._emulator._emul_i

        # Open hdf5-file
        with PRISM_File('r+') as file:
            # Loop over entire provided data dict
            for keyword, data in data_dict.items():
                # Log what data is being saved
                logger.info("Saving %s data at iteration %s to HDF5."
                            % (keyword, emul_i))

                # Check what data keyword has been provided
                # IMPL_CUT
                if(keyword == 'impl_cut'):
                    # Check if impl_cut data has been saved before
                    try:
                        self._impl_cut[emul_i] = data[0]
                        self._cut_idx[emul_i] = data[1]
                    except IndexError:
                        self._impl_cut.append(data[0])
                        self._cut_idx.append(data[1])
                    finally:
                        file['%s' % (emul_i)].attrs['impl_cut'] = data[0]
                        file['%s' % (emul_i)].attrs['cut_idx'] = data[1]

                # IMPL_SAM
                elif(keyword == 'impl_sam'):
                    # Check if any plausible regions have been found at all
                    n_impl_sam = np.shape(data)[0]
                    prc = 1 if(n_impl_sam != 0) else 0

                    # Check if impl_sam data has been saved before
                    try:
                        self._n_impl_sam[emul_i] = n_impl_sam
                    except IndexError:
                        file.create_dataset('%s/impl_sam' % (emul_i),
                                            data=data)
                        self._n_impl_sam.append(n_impl_sam)
                    else:
                        del file['%s/impl_sam' % (emul_i)]
                        file.create_dataset('%s/impl_sam' % (emul_i),
                                            data=data)
                    finally:
                        self._prc = prc
                        self._impl_sam = data
                        file['%s' % (emul_i)].attrs['prc'] = bool(prc)
                        file['%s' % (emul_i)].attrs['n_impl_sam'] = n_impl_sam

                # N_EVAL_SAM
                elif(keyword == 'n_eval_sam'):
                    # Check if n_eval_sam has been saved before
                    try:
                        self._n_eval_sam[emul_i] = data
                    except IndexError:
                        self._n_eval_sam.append(data)
                    finally:
                        file['%s' % (emul_i)].attrs['n_eval_sam'] = data

                # INVALID KEYWORD
                else:
                    logger.error("Invalid keyword argument provided!")
                    raise ValueError("Invalid keyword argument provided!")

    # This function saves a statistic to hdf5
    @docstring_substitute(emul_i=std_emul_i_doc)
    def _save_statistics(self, emul_i, stat_dict):
        """
        Saves a given statistics dict {`keyword`: [`value`, `unit`]} at
        emulator iteration `emul_i` to the HDF5-file. The provided values are
        always saved as strings.

        Parameters
        ----------
        %(emul_i)s

        Dict Variables
        --------------
        keyword : str
            String containing the name/keyword of the statistic that is being
            saved.
        value : int, float or str
            The value of the statistic.
        unit : str
            The unit of the statistic.

        """

        # Do logging
        logger = logging.getLogger('STATISTICS')
        logger.info("Saving statistics to HDF5.")

        # Open hdf5-file
        with PRISM_File('r+') as file:
            # Save statistics
            for keyword, (value, unit) in stat_dict.items():
                file['%s/statistics' % (emul_i)].attrs[keyword] =\
                    [str(value).encode('ascii', 'ignore'),
                     unit.encode('ascii', 'ignore')]

    # This is function 'k'
    # Reminder that this function should only be called once per sample set
    # TODO: Allow variable n_sam for each data point? More versatile and chaos
    @docstring_substitute(emul_i=std_emul_i_doc)
    def _evaluate_model(self, emul_i, sam_set, ext_sam_set, ext_mod_set):
        """
        Evaluates the model at all specified model evaluation samples at a
        given emulator iteration `emul_i`.

        Parameters
        ----------
        %(emul_i)s
        sam_set : 2D :obj:`~numpy.ndarray` object
            Array containing the model evaluation samples.
        ext_sam_set : 1D or 2D :obj:`~numpy.ndarray` object
            Array containing the externally provided model evaluation samples.
        ext_mod_set : 1D or 2D :obj:`~numpy.ndarray` object
            Array containing the model outputs of all specified externally
            provided model evaluation samples.

        Generates
        ---------
        sam_set : 2D :obj:`~numpy.ndarray` object
            Array containing the model evaluation samples for emulator
            iteration `emul_i`.
        mod_set : 2D :obj:`~numpy.ndarray` object
            Array containing the model outputs of all specified model
            evaluation samples for emulator iteration `emul_i`.

        """

        # Controller does logging
        if self._is_controller:
            # Log that evaluation of model samples is started
            logger = logging.getLogger('MODEL')
            logger.info("Evaluating model samples.")

            # Do model evaluations
            start_time = time()

        # Obtain number of samples
        n_sam = np.shape(sam_set)[0]

        # Check who needs to call the model
        if(self._modellink._MPI_call or
           (not self._modellink._MPI_call and self._is_controller)):
            # Request all evaluation samples at once
            if self._modellink._multi_call:
                mod_set = self._multi_call_model(emul_i, sam_set)

            # Request evaluation samples one-by-one
            else:
                # Initialize mod_set
                mod_set = np.zeros([self._modellink._n_data, n_sam])

                # Loop over all requested evaluation samples
                for i in range(n_sam):
                    mod_set[:, i] = self._call_model(emul_i, sam_set[i])

        # Controller finishing up
        if self._is_controller:
            # Get end time
            end_time = time()-start_time

            # Check if ext_real_set was provided
            if(np.shape(ext_sam_set)[0] != 0):
                sam_set = np.concatenate([sam_set, ext_sam_set], axis=0)
                mod_set = np.concatenate([mod_set, ext_mod_set], axis=1)
                use_ext_real_set = 1
            else:
                use_ext_real_set = 0

            # Save data to hdf5
            if(emul_i == 1 or self._emulator._emul_type == 'default'):
                self._emulator._save_data(emul_i, {
                    'mod_real_set': [sam_set, mod_set, use_ext_real_set]})
            else:
                raise NotImplementedError

            # Log that this is finished
            self._save_statistics(emul_i, {
                'tot_model_eval_time': ['%.2f' % (end_time), 's'],
                'avg_model_eval_time': ['%.3g' % (end_time/n_sam), 's'],
                'MPI_comm_size_model': ['%i' % (self._size), '']})
            print("Finished evaluating model samples in %.2f seconds, "
                  "averaging %.3g seconds per model evaluation."
                  % (end_time, end_time/n_sam))
            logger.info("Finished evaluating model samples in %.2f seconds, "
                        "averaging %.3g seconds per model evaluation."
                        % (end_time, end_time/n_sam))

        # MPI Barrier
        MPI.COMM_WORLD.Barrier()

    # This function generates a large Latin Hypercube sample set to evaluate
    # the emulator at
    # TODO: Maybe make sure that n_sam_init samples are used for next iteration
    # This can be done by evaluating a 1000 samples in the emulator, check how
    # many survive and then use an LHD with the number of samples required to
    # let n_sam_init samples survive.
    @docstring_substitute(emul_i=std_emul_i_doc)
    def _get_eval_sam_set(self, emul_i):
        """
        Generates an emulator evaluation sample set to be used for updating an
        emulator iteration. Currently uses the
        :func:`~e13tools.sampling.lhd` function.

        Parameters
        ----------
        %(emul_i)s

        Returns
        -------
        eval_sam_set : 2D :obj:`~numpy.ndarray` object
            Array containing the evaluation samples.

        """

        # Log about this
        logger = logging.getLogger('EVAL_SAMS')

        # Obtain number of samples
        n_eval_sam = self._get_n_eval_sam(emul_i)

        # Create array containing all new samples to evaluate with emulator
        logger.info("Creating emulator evaluation sample set with size %s."
                    % (n_eval_sam))
        eval_sam_set = lhd(n_eval_sam, self._modellink._n_par,
                           self._modellink._par_rng, 'center',
                           self._criterion, 100,
                           constraints=self._emulator._sam_set[emul_i])
        logger.info("Finished creating sample set.")

        # Return it
        return(eval_sam_set)

    # This function performs an implausibility cut-off check on a given sample
    # TODO: Implement dynamic impl_cut
    @staticmethod
    @docstring_substitute(emul_i=std_emul_i_doc)
    def _do_impl_check(obj, emul_i, uni_impl_val):
        """
        Performs an implausibility cut-off check on the provided implausibility
        values `uni_impl_val` at emulator iteration `emul_i`, using the
        impl_cut values given in `obj`.

        Parameters
        ----------
        obj : :obj:`~Pipeline` object or :obj:`~Projection` object
            Instance of the :class:`~Pipeline` class or :class:`~Projection`
            class.
        %(emul_i)s
        uni_impl_val : 1D array_like
            Array containing all univariate implausibility values corresponding
            to a certain parameter set for all data points.

        Returns
        -------
        result : bool
            *True* if check was successful, *False* if it was not.
        impl_cut_val : float
            Implausibility value at the first real implausibility cut-off.

        """

        # Sort impl_val to compare with the impl_cut list
        # TODO: Maybe use np.partition here?
        sorted_impl_val = np.flip(np.sort(uni_impl_val, axis=-1), axis=-1)

        # Save the implausibility value at the first real cut-off
        impl_cut_val = sorted_impl_val[obj._cut_idx[emul_i]]

        # Scan over all data points in this sample
        for impl_val, cut_val in zip(sorted_impl_val, obj._impl_cut[emul_i]):
            # If impl_cut is not 0 and impl_val is not below impl_cut, break
            if(cut_val != 0 and impl_val > cut_val):
                return(0, impl_cut_val)
        else:
            # If for-loop ended in a normal way, the check was successful
            return(1, impl_cut_val)

    # This is function 'I²(x)'
    # This function calculates the univariate implausibility values
    # TODO: Introduce check if emulator variance is much lower than other two
    # TODO: Alternatively, remove covariance calculations when this happens
    # TODO: Parameter uncertainty should be implemented at some point
    @docstring_substitute(emul_i=std_emul_i_doc)
    def _get_uni_impl(self, emul_i, adj_exp_val, adj_var_val):
        """
        Calculates the univariate implausibility values at a given emulator
        iteration `emul_i` for specified expectation and variance values
        `adj_exp_val` and `adj_var_val`.

        Parameters
        ----------
        %(emul_i)s
        adj_exp_val, adj_var_val : 1D array_like
            The adjusted expectation and variance values to calculate the
            univeriate implausibility for.

        Returns
        -------
        uni_impl_val : 1D :obj:`~numpy.ndarray` object
            Univariate implausibility value for every data point.

        """

        # Obtain model discrepancy variance
        md_var = self._get_md_var(emul_i)

        # Initialize empty univariate implausibility
        uni_impl_val_sq = np.zeros(self._emulator._n_data[emul_i])

        # Calculate the univariate implausibility values
        for i in range(self._emulator._n_data[emul_i]):
            uni_impl_val_sq[i] =\
                pow(adj_exp_val[i]-self._emulator._data_val[emul_i][i], 2) /\
                (adj_var_val[i]+md_var[i] +
                 pow(self._emulator._data_err[emul_i][i], 2))

        # Take square root
        uni_impl_val = np.sqrt(uni_impl_val_sq)

        # Return it
        return(uni_impl_val)

    # This function calculates the model discrepancy variance
    # Basically takes all uncertainties of Sec. 3.1 of Vernon into account that
    # are not already in the emulator ([3] and [5])
    @docstring_substitute(emul_i=std_emul_i_doc)
    def _get_md_var(self, emul_i):
        """
        Retrieves the model discrepancy variance, which includes all variances
        that are created by the model provided by the :obj:`~ModelLink`
        instance. This method tries to call the :meth:`~ModelLink.get_md_var`
        method, and assumes a default model discrepancy variance of 1/6th the
        data value if it cannot be called.

        Parameters
        ----------
        %(emul_i)s

        Returns
        -------
        var_md : 1D :obj:`~numpy.ndarray` object
            Variance of the model discrepancy.

        """

        # Obtain md variances
        # Try to use the user-defined md variances
        try:
            md_var =\
                self._modellink.get_md_var(emul_i,
                                           self._emulator._data_idx[emul_i])

        # If it was not user-defined, use a default value
        except NotImplementedError:
            # Use factor 2 difference on 2 sigma as acceptable
            # Imagine that 2 sigma range is given if lower and upper are factor
            # 2 apart. This gives that sigma must be 1/6th of the data value
            md_var = pow(np.array(self._emulator._data_val[emul_i])/6, 2)

        # Check if all values are non-negative floats
        for value in md_var:
            check_nneg_float(value, 'md_var')

        # Return it
        return(md_var)

    # This function completes the list of implausibility cut-offs
    @staticmethod
    def _get_impl_cut(obj, impl_cut, temp):
        """
        Generates the full list of impl_cut-offs from the incomplete, shortened
        `impl_cut` list and saves them in the given `obj`.

        Parameters
        ----------
        obj : :obj:`~Pipeline` object or :obj:`~Projection` object
            Instance of the :class:`~Pipeline` class or :class:`~Projection`
            class.
        impl_cut : 1D list
            Incomplete, shortened impl_cut-offs list provided during class
            initialization.
        temp : bool
            Whether the implausibility parameters should only be stored in
            memory (*True*) or should also be saved to HDF5 (*False*).

        Generates
        ---------
        impl_cut : 1D :obj:`~numpy.ndarray` object
            Full list containing the impl_cut-offs for all data points provided
            to the emulator.
        cut_idx : int
            Index of the first impl_cut-off in the impl_cut list that is not
            a wildcard.

        """

        # Log that impl_cut-off list is being acquired
        logger = logging.getLogger('INIT')
        logger.info("Generating full implausibility cut-off list.")

        # Complete the impl_cut list
        impl_cut[0] = check_nneg_float(impl_cut[0], 'impl_cut[0]')
        for i in range(1, len(impl_cut)):
            impl_cut[i] = check_nneg_float(impl_cut[i], 'impl_cut[%s]' % (i))
            if(impl_cut[i] == 0):
                impl_cut[i] = impl_cut[i-1]
            elif(impl_cut[i-1] != 0 and impl_cut[i] > impl_cut[i-1]):
                raise ValueError("Cut-off %s is higher than cut-off %s "
                                 "(%s > %s)" % (i, i-1, impl_cut[i],
                                                impl_cut[i-1]))

        # Get the index identifying where the first real impl_cut is
        for i, impl in enumerate(impl_cut):
            if(impl != 0):
                cut_idx = i
                break
        else:
            raise ValueError("No non-wildcard implausibility cut-off is "
                             "provided!")

        # Save both impl_cut and cut_idx
        if temp:
            # If they need to be stored temporarily
            obj._impl_cut.append(np.array(impl_cut))
            obj._cut_idx.append(cut_idx)
        else:
            obj._save_data({
                'impl_cut': [np.array(impl_cut), cut_idx]})

        # Log end of process
        logger.info("Finished generating implausibility cut-off list.")

    # This function reads in the impl_cut list from the PRISM parameters file
    # TODO: Make impl_cut dynamic
    def _get_impl_par(self, temp):
        """
        Reads in the impl_cut list and other parameters for implausibility
        evaluations from the PRISM parameters file and saves them in the last
        emulator iteration.

        Parameters
        ----------
        temp : bool
            Whether the implausibility parameters should only be stored in
            memory (*True*) or should also be saved to HDF5 (*False*).

        Generates
        ---------
        impl_cut : 1D :obj:`~numpy.ndarray` object
            Full list containing the impl_cut-offs for all data points provided
            to the emulator.
        cut_idx : int
            Index of the first impl_cut-off in the impl_cut list that is not
            0.

        """

        # Controller only
        if self._is_controller:
            # Do some logging
            logger = logging.getLogger('INIT')
            logger.info("Obtaining implausibility analysis parameters.")

            # Obtaining default pipeline parameter dict
            par_dict = self._get_default_parameters()

            # Read in data from provided PRISM parameters file
            if self._prism_file is not None:
                pipe_par = np.genfromtxt(self._prism_file, dtype=(str),
                                         delimiter=':', autostrip=True)

                # Make sure that pipe_par is 2D
                pipe_par = np.array(pipe_par, ndmin=2)

                # Combine default parameters with read-in parameters
                par_dict.update(pipe_par)

            # More logging
            logger.info("Checking compatibility of provided implausibility "
                        "analysis parameters.")

            # Implausibility cut-off
            # Remove all unwanted characters from the string and split it up
            impl_cut_str = convert_str_seq(par_dict['impl_cut'])

            # Convert list of strings to list of floats and perform completion
            self._get_impl_cut(
                self, list(float(impl_cut) for impl_cut in impl_cut_str), temp)

            # Finish logging
            logger.info("Finished obtaining implausibility analysis "
                        "parameters.")

    # This function processes an externally provided real_set
    # TODO: Perform maximin/mincorr analysis on provided samples?
    def _get_ext_real_set(self, ext_real_set):
        """
        Processes an externally provided model realization set `ext_real_set`,
        containing the used sample set and the corresponding data value set.

        Parameters
        ----------
        ext_real_set : list, dict or None
            List of arrays containing an externally calculated set of model
            evaluation samples and its data values, a dict with keys
            [`sam_set`, `mod_set`] containing these arrays or *None* if no
            external set needs to be used.

        Returns
        -------
        ext_sam_set : 1D or 2D :obj:`~numpy.ndarray` object
            Array containing the externally provided model evaluation samples.
        ext_mod_set : 1D or 2D :obj:`~numpy.ndarray` object
            Array containing the model outputs of all specified externally
            provided model evaluation samples.

        """

        # If no ext_real_set is provided, return empty arrays without logging
        if ext_real_set is None:
            return(np.array([]), np.array([]))

        # Do some logging
        logger = logging.getLogger('INIT')
        logger.info("Processing externally provided model realization set.")

        # If a list is given
        if isinstance(ext_real_set, list):
            # Check if ext_real_set contains 2 elements
            if(len(ext_real_set) != 2):
                logger.error("Input argument 'ext_real_set' is not of length "
                             "2!")
                raise ShapeError("Input argument 'ext_real_set' is not of "
                                 "length 2!")

            # Try to extract ext_sam_set and ext_mod_set
            try:
                ext_sam_set = ext_real_set[0]
                ext_mod_set = ext_real_set[1]
            except Exception as error:
                logger.error("Input argument 'ext_real_set' is invalid (%s)!"
                             % (error))
                raise InputError("Input argument 'ext_real_set' is invalid "
                                 "(%s)!" % (error))

        # If a dict is given
        elif isinstance(ext_real_set, dict):
            # Check if ext_real_set contains correct keys
            if 'sam_set' not in ext_real_set.keys():
                logger.error("Input argument 'ext_real_set' does not contain "
                             "key 'sam_set'!")
                raise KeyError("Input argument 'ext_real_set' does not contain"
                               " key 'sam_set'!")
            if 'mod_set' not in ext_real_set.keys():
                logger.error("Input argument 'ext_real_set' does not contain "
                             "key 'mod_set'!")
                raise KeyError("Input argument 'ext_real_set' does not contain"
                               " key 'mod_set'!")

            # Try to extract ext_sam_set and ext_mod_set
            try:
                ext_sam_set = ext_real_set['sam_set']
                ext_mod_set = ext_real_set['mod_set']
            except Exception as error:
                logger.error("Input argument 'ext_real_set' is invalid (%s)!"
                             % (error))
                raise InputError("Input argument 'ext_real_set' is invalid "
                                 "(%s)!" % (error))

        # If anything else is given
        else:
            logger.error("Input argument 'ext_real_set' is invalid!")
            raise InputError("Input argument 'ext_real_set' is invalid!")

        # Check if ext_sam_set and ext_mod_set can be converted to NumPy arrays
        try:
            ext_sam_set = np.array(ext_sam_set, ndmin=2)
            ext_mod_set = np.array(ext_mod_set, ndmin=2)
        except Exception as error:
            logger.error("Input argument 'ext_real_set' is invalid (%s)!"
                         % (error))
            raise InputError("Input argument 'ext_real_set' is invalid (%s)!"
                             % (error))

        # Check if ext_sam_set and ext_mod_set have correct shapes
        if not(ext_sam_set.shape[1] == self._modellink._n_par):
            logger.error("External sample set has incorrect number of "
                         "parameters (%s != %s)!"
                         % (ext_sam_set.shape[1], self._modellink._n_par))
            raise ShapeError("External sample set has incorrect number of "
                             "parameters (%s != %s)!"
                             % (ext_sam_set.shape[1], self._modellink._n_par))
        if not(ext_mod_set.shape[1] == self._modellink._n_data):
            logger.error("External model output set has incorrect number of "
                         "data values (%s != %s)!"
                         % (ext_mod_set.shape[1], self._modellink._n_data))
            raise ShapeError("External model output set has incorrect number "
                             "of data values (%s != %s)!"
                             % (ext_mod_set.shape[1], self._modellink._n_data))
        if not(ext_sam_set.shape[0] == ext_mod_set.shape[0]):
            logger.error("External sample and model output sets do not contain"
                         " the same number of samples (%s != %s)!"
                         % (ext_sam_set.shape[0], ext_mod_set.shape[0]))

        # Check if ext_sam_set and ext_mod_set solely contain floats
        for i, (par_set, mod_out) in enumerate(zip(ext_sam_set, ext_mod_set)):
            for j, (par, out) in enumerate(zip(par_set, mod_out)):
                check_float(par, 'ext_sam_set[%s, %s]' % (i, j))
                check_float(out, 'ext_mod_set[%s, %s]' % (i, j))

        # Check if all samples are within parameter space
        lower_bnd = self._modellink._par_rng[:, 0]
        upper_bnd = self._modellink._par_rng[:, 1]
        for i, par_set in enumerate(ext_sam_set):
            if not(((lower_bnd <= par_set)*(par_set <= upper_bnd)).all()):
                logger.error("External sample set contains a sample outside of"
                             " parameter space at index %s!" % (i))
                raise ValueError("External sample set contains a sample "
                                 "outside of parameter space at index %s!"
                                 % (i))

        # Log that processing has been finished
        logger.info("Finished processing externally provided model realization"
                    " set of size %s." % (ext_sam_set.shape[0]))

        # If all checks are passed, return ext_sam_set and ext_mod_set
        return(ext_sam_set, ext_mod_set.T)


# %% VISIBLE CLASS METHODS
    # This function analyzes the emulator and determines the plausible regions
    # TODO: Implement check if impl_idx is big enough to be used in next emul_i
    def analyze(self):
        """
        Analyzes the emulator system at the last emulator iteration for a large
        number of emulator evaluation samples. All samples that survive the
        implausibility checks, are used in the construction of the next
        emulator iteration.

        Generates
        ---------
        impl_sam : 2D :obj:`~numpy.ndarray` object
            Array containing all emulator evaluation samples that survived the
            implausibility checks.
        prc : bool
            Bool indicating whether or not plausible regions have been found
            during this analysis.

        """

        # Only controller
        if self._is_controller:
            # Begin logging
            logger = logging.getLogger('ANALYZE')

            # Save current time
            start_time1 = time()

            # Get emul_i
            emul_i = self._emulator._get_emul_i(None)

            # Begin analyzing
            logger.info("Analyzing emulator system at iteration %s."
                        % (emul_i))

            # Get the impl_cut list
            self._get_impl_par(False)

            try:
                # Create an emulator evaluation sample set
                eval_sam_set = self._get_eval_sam_set(emul_i)
                n_eval_sam = eval_sam_set.shape[0]

                # Create empty list for indices of samples that pass impl_check
                impl_idx = []

                # Save current time again
                start_time2 = time()

                # Default emulator
                if(self._emulator._emul_type == 'default'):
                    # Calculate exp, var, impl for these samples
                    for i, par_set in enumerate(eval_sam_set):
                        for j in range(1, emul_i+1):
                            # Obtain implausibility
                            adj_val = self._emulator._evaluate(j, par_set)
                            uni_impl_val = self._get_uni_impl(j, *adj_val)

                            # Do implausibility cut-off check
                            # If check is unsuccessful, break inner for-loop
                            # and skip save
                            if not self._do_impl_check(self, j,
                                                       uni_impl_val)[0]:
                                break

                        # If check was successful, save corresponding index
                        else:
                            impl_idx.append(i)

                else:
                    raise NotImplementedError

                # Obtain some timers
                end_time = time()
                time_diff_total = end_time-start_time1
                time_diff_eval = end_time-start_time2

                # Save the results
                self._save_data({
                    'impl_sam': eval_sam_set[impl_idx],
                    'n_eval_sam': n_eval_sam})
            except KeyboardInterrupt:
                logger.info("Emulator system analysis has been interrupted by "
                            "user.")
                print("Emulator system analysis has been interrupted by user.")
            else:
                # Save statistics about anal time, evaluation speed, par_space
                avg_eval_rate = n_eval_sam/time_diff_eval
                par_space_rem = (len(impl_idx)/n_eval_sam)*100
                self._save_statistics(emul_i, {
                    'tot_analyze_time': ['%.2f' % (time_diff_total), 's'],
                    'avg_emul_eval_rate': ['%.2f' % (avg_eval_rate), '1/s'],
                    'par_space_remaining': ['%.3g' % (par_space_rem), '%'],
                    'MPI_comm_size_anal': ['%i' % (self._size), '']})

                # Log that analysis has been finished and save their statistics
                print("Finished analysis of emulator system in %.2f seconds, "
                      "averaging %.2f emulator evaluations per second."
                      % (time_diff_total, n_eval_sam/time_diff_eval))
                print("There is %.3g%% of parameter space remaining."
                      % ((len(impl_idx)/n_eval_sam)*100))
                logger.info("Finished analysis of emulator system in %.2f "
                            "seconds, averaging %.2f emulator evaluations per "
                            "second."
                            % (time_diff_total, n_eval_sam/time_diff_eval))
                logger.info("There is %.3g%% of parameter space remaining."
                            % ((len(impl_idx)/n_eval_sam)*100))

        # Display details about current state of pipeline
        self.details()

    # This function constructs a specified iteration of the emulator system
    # TODO: Make time and RAM cost plots
    # TODO: Fix the timers for interrupted constructs
    @docstring_substitute(emul_i=call_emul_i_doc)
    def construct(self, emul_i=None, analyze=True, ext_init_set=None,
                  force=False):
        """
        Constructs the emulator at the specified emulator iteration `emul_i`,
        and performs an implausibility analysis on the emulator systems right
        afterward if requested (:meth:`~analyze`).

        Optional
        --------
        %(emul_i)s
        analyze : bool. Default: True
            Bool indicating whether or not to perform an analysis after the
            specified emulator iteration has been successfully constructed,
            which is required for constructing the next iteration.
        ext_init_set : list, dict or None. Default: None
            List of arrays containing an externally calculated set of initial
            model evaluation samples and its data values, a dict with keys
            [`sam_set`, `mod_set`] containing these arrays or *None* if no
            external realization set needs to be used.
            This parameter has no use for `emul_i` != 1.
        force : bool. Default: False
            Controls what to do if the specified emulator iteration `emul_i`
            already (partly) exists.
            If *False*, finish construction of the specified iteration or skip
            it if already finished.
            If *True*, reconstruct the specified iteration entirely.

        Generates
        ---------
        A new HDF5-group with the emulator iteration value as its name, in the
        loaded emulator file, containing emulator data required for this
        emulator iteration.

        Notes
        -----
        Using an emulator iteration that has been (partly) constructed before,
        will finish that construction or skip construction if already finished
        when `force` = *False; or it will delete that and all following
        iterations, and reconstruct the specified iteration when `force` =
        *True*. Using `emul_i` = 1 and `force` = *True* is equivalent to
        reconstructing the entire emulator system.

        If no implausibility analysis is requested, then the implausibility
        parameters are read in from the PRISM parameters file and temporarily
        stored in memory in order to enable the usage of the :meth:`~evaluate`
        method.

        """

        # Log that a new emulator iteration is being constructed
        logger = logging.getLogger('CONSTRUCT')

        # Only the controller should run this
        if self._is_controller:
            # Save current time
            start_time = time()

            # Check if force-parameter received a bool
            force = check_bool(force, 'force')

            # Set emul_i correctly
            if emul_i is None:
                emul_i = self._emulator._emul_i+1
            elif(emul_i == 1):
                pass
            else:
                emul_i = self._emulator._get_emul_i(emul_i-1)+1

            # Check if iteration was interrupted or not, or if force is True
            logger.info("Checking state of emulator iteration %s." % (emul_i))
            try:
                # If force is True, reconstruct full iteration
                if force:
                    logger.info("Emulator iteration %s has been requested to "
                                "be reconstructed." % (emul_i))
                    c_from_start = 1

                # If interrupted at start, reconstruct full iteration
                elif('mod_real_set' in self._emulator._ccheck[emul_i]):
                    logger.info("Emulator iteration %s does not contain "
                                "evaluated model realization data. Will be "
                                "constructed from start." % (emul_i))
                    c_from_start = 1

                # If interrupted midway, do not reconstruct full iteration
                else:
                    logger.info("Construction of emulator iteration %s was "
                                "interrupted. Continuing from point of "
                                "interruption." % (emul_i))
                    c_from_start = 0

            # If never constructed before, construct full iteration
            except IndexError:
                logger.info("Emulator iteration %s has not been constructed."
                            % (emul_i))
                c_from_start = 1

        # Remaining workers
        else:
            c_from_start = None

        # Check if analyze-parameter received a bool
        analyze = check_bool(analyze, 'analyze')

        # Broadcast emul_i to workers
        emul_i = MPI.COMM_WORLD.bcast(emul_i, 0)

        # Broadcast construct_emul_i to workers
        c_from_start = MPI.COMM_WORLD.bcast(c_from_start, 0)

        # If iteration needs to be constructed completely, create/prepare it
        if c_from_start:
            # Controller only
            if self._is_controller:
                # Log that construction of emulator iteration is being started
                logger.info("Starting construction of emulator iteration %s."
                            % (emul_i))

                # Check emul_i and act accordingly
                if(emul_i == 1):
                    # Process ext_init_set
                    ext_sam_set, ext_mod_set =\
                        self._get_ext_real_set(ext_init_set)

                    # Obtain number of externally provided model realizations
                    n_ext_sam = np.shape(ext_sam_set)[0]

                    # Create a new emulator system
                    self._emulator._create_new_emulator()

                    # Reload the data
                    self._load_data()

                    # Create initial set of model evaluation samples
                    n_sam_init = max(0, self._n_sam_init-n_ext_sam)
                    if n_sam_init:
                        logger.info("Creating initial model evaluation sample "
                                    "set of size %s." % (n_sam_init))
                        add_sam_set = lhd(n_sam_init, self._modellink._n_par,
                                          self._modellink._par_rng, 'center',
                                          self._criterion,
                                          constraints=ext_sam_set)
                        logger.info("Finished creating initial sample set.")

                else:
                    # Get dummy ext_real_set
                    ext_sam_set, ext_mod_set = self._get_ext_real_set(None)

                    # Check if previous iteration was analyzed, do so if not
                    if not self._n_eval_sam[emul_i-1]:
                        # Let workers know that emulator needs analyzing
                        for rank in range(1, self._size):
                            MPI.COMM_WORLD.send(1, dest=rank, tag=999+rank)

                        # Analyze previous iteration
                        logger.info("Previous emulator iteration has not been "
                                    "analyzed. Performing analysis first.")
                        self.analyze()
                    else:
                        # If not, let workers know
                        for rank in range(1, self._size):
                            MPI.COMM_WORLD.send(0, dest=rank, tag=999+rank)

                    # Check if a new emulator iteration can be constructed
                    if not self._prc:
                        logger.error("No plausible regions were found in the "
                                     "analysis of the previous emulator "
                                     "iteration. Construction is not "
                                     "possible!")
                        raise RequestError("No plausible regions were found in"
                                           " the analysis of the previous "
                                           "emulator iteration. Construction "
                                           "is not possible!")

                    # Make the emulator prepare for a new iteration
                    reload = self._emulator._prepare_new_iteration(emul_i)

                    # Make sure the correct pipeline data is loaded in
                    if reload:
                        self._load_data()

                    # Obtain additional sam_set
                    add_sam_set = self._impl_sam

            # Remaining workers
            else:
                # Listen for calls from controller during emulator creation
                if(emul_i == 1):
                    # Check if mock_data is requested
                    get_mock = MPI.COMM_WORLD.recv(source=0,
                                                   tag=999+self._rank)

                    # If mock_data is requested, call for it
                    if get_mock:
                        self._get_mock_data()

                # Listen for calls from controller during any other iteration
                else:
                    # Check if analysis is required
                    do_analyze = MPI.COMM_WORLD.recv(source=0,
                                                     tag=999+self._rank)

                    # If previous iteration needs analyzing, call analyze()
                    if do_analyze:
                        self.analyze()

                # All workers get dummy sets
                add_sam_set = []
                ext_sam_set = []
                ext_mod_set = []

            # MPI Barrier to free up workers
            MPI.COMM_WORLD.Barrier()

            # Broadcast add_sam_set to workers
            add_sam_set = MPI.COMM_WORLD.bcast(add_sam_set, 0)

            # Obtain corresponding set of model evaluations
            self._evaluate_model(emul_i, add_sam_set, ext_sam_set, ext_mod_set)

        # Only controller
        if self._is_controller:
            # Construct emulator
            self._emulator._construct_iteration(emul_i)

            # Save that emulator system has not been analyzed yet
            self._save_data({
                'impl_sam': [],
                'n_eval_sam': 0})

            # Log that construction has been completed
            time_diff_total = time()-start_time
            self._save_statistics(emul_i, {
                'tot_construct_time': ['%.2f' % (time_diff_total), 's']})
            print("Finished construction of emulator system in %.2f seconds."
                  % (time_diff_total))
            logger.info("Finished construction of emulator system in %.2f "
                        "seconds." % (time_diff_total))

        # Analyze the emulator system if requested
        if analyze:
            self.analyze()
        else:
            self._get_impl_par(True)
            self.details(emul_i)

    # This function allows one to obtain the pipeline details/properties
    # TODO: Allow the viewing of the entire polynomial function in SymPy
    @docstring_substitute(emul_i=user_emul_i_doc)
    def details(self, emul_i=None):
        """
        Prints the details/properties of the currently loaded pipeline instance
        at given emulator iteration `emul_i`. See ``Notes`` for detailed
        descriptions of all printed properties.

        Optional
        --------
        %(emul_i)s

        Notes
        -----
        HDF5-file name
            The relative path to the loaded HDF5-file starting at `root_dir`,
            which consists of `working_dir` and `hdf5_file`.
        Emulator type
            The type of this emulator system, corresponding to the provided
            `emul_type` during :class:`~Pipeline` initialization.
        ModelLink subclass
            Name of the :class:`~ModelLink` subclass used to construct this
            emulator system.
        Emulation method
            Indicates the combination of regression and Gaussian emulation
            methods that have been used for this emulator system.
        Mock data used?
            Whether or not mock data has been used to construct this emulator
            system. If so, the printed estimates for all model parameters are
            the parameter values used to create the mock data.

        Emulator iteration
            The iteration of the emulator system this details overview is
            about. By default, this is the last constructed iteration.
        Construction completed?
            Whether or not the construction of this emulator iteration is
            completed. If not, the missing components are listed and the
            remaining information of this iteration is not printed.
        Plausible regions?
            Whether or not plausible regions have been found during the
            analysis of this emulator iteration. If no analysis has been done
            yet, "N/A" will be printed.
        Projections available?
            Whether or not projections have been created for this emulator
            iteration. If projections are available and analysis has been done,
            but with different implausibility cut-offs, a "desync" note is
            added. Also prints number of available projections versus maximum
            number of projections in brackets.

        # of model evaluation samples
            The total number of model evaluation samples used to construct all
            emulator iterations up to this iteration, with the number for every
            individual iteration in brackets.
        # of plausible/analyzed samples
            The number of emulator evaluation samples that passed the
            implausibility check out of the total number of analyzed samples in
            this emulator iteration.
            This is the number of model evaluation samples that was/will be
            used for the construction of the next emulator iteration.
            If no analysis has been done, the numbers show up as "-".
        %% of parameter space remaining
            The percentage of the total number of analyzed samples that passed
            the implausibility check in this emulator iteration.
            If no analysis has been done, the number shows up as "-".
        # of active/total parameters
            The number of model parameters that was considered active during
            the construction of this emulator iteration, compared to the total
            number of model parameters defined in the used :class:`~ModelLink`
            subclass.
        # of emulated data points
            The number of data points that have been emulated in this
            emulator iteration.

        Parameter space
            Lists the name, lower and upper value boundaries and estimate (if
            provided) of all model parameters defined in the used
            :class:`~ModelLink` subclass. An asterisk is printed in front of
            the parameter name if this model parameter was considered active
            during the construction of this emulator iteration. A question mark
            is used instead if the construction of this emulator iteration is
            not finished.

        """

        # Only controller
        if self._is_controller:
            # Define details logger
            logger = logging.getLogger("DETAILS")
            logger.info("Collecting details about current pipeline instance.")

            # Check what kind of hdf5-file was provided
            try:
                if len(self._emulator._ccheck[-1]):
                    if emul_i is None:
                        emul_i = self._emulator._emul_i+1
                    elif(emul_i == self._emulator._emul_i+1):
                        pass
                    else:
                        emul_i = self._emulator._get_emul_i(emul_i)
                else:
                    emul_i = self._emulator._get_emul_i(emul_i)
            except RequestError:
                # MPI Barrier for controller to sync with workers at the end
                MPI.COMM_WORLD.Barrier()
                return
            else:
                # Get max lengths of various strings for parameter section
                name_len =\
                    max([len(par_name) for par_name in
                         self._modellink._par_name])
                lower_len =\
                    max([len(str(i)) for i in self._modellink._par_rng[:, 0]])
                upper_len =\
                    max([len(str(i)) for i in self._modellink._par_rng[:, 1]])
                est_len =\
                    max([len('%.5f' % (i)) for i in self._modellink._par_est
                         if i is not None])

                # Open hdf5-file
                with PRISM_File('r') as file:
                    # Check if projection data is available
                    try:
                        file['%s/proj_hcube' % (emul_i)]
                    except KeyError:
                        proj = 0
                        n_proj = 0

                    # If projection data is available
                    else:
                        n_proj = len(file['%s/proj_hcube' % (emul_i)].keys())
                        proj_impl_cut =\
                            file['%s/proj_hcube' % (emul_i)].attrs['impl_cut']
                        proj_cut_idx =\
                            file['%s/proj_hcube' % (emul_i)].attrs['cut_idx']

                        # Check if projections were made with the same impl_cut
                        try:
                            # If it was, projections are synced
                            if((proj_impl_cut == self._impl_cut[emul_i]).all()
                               and proj_cut_idx == self._cut_idx[emul_i]):
                                proj = 1

                            # If not, projections are desynced
                            else:
                                proj = 2

                        # If analysis was never done, projections are synced
                        except IndexError:
                            proj = 1

            # Determine the number of (active) parameters
            n_par = self._modellink._n_par
            n_active_par = len(self._emulator._active_par[emul_i])

            # Calculate the maximum number of projections
            n_proj_max = nCr(n_active_par, 1 if(n_par == 2) else 2)

            # Log file being closed
            logger.info("Finished collecting details about current pipeline "
                        "instance.")

            # Set width of detail names
            width = 31

            # PRINT DETAILS
            # HEADER
            print("\n")
            print("PIPELINE DETAILS")
            print("="*width)

            # GENERAL
            print("\nGENERAL")
            print("-"*width)

            # General details about loaded emulator system
            print("{0: <{1}}\t'{2}'".format("HDF5-file name", width,
                                            self._hdf5_file_name))
            print("{0: <{1}}\t'{2}'".format("Emulator type", width,
                                            self._emulator._emul_type))
            print("{0: <{1}}\t{2}".format("ModelLink subclass", width,
                                          self._modellink._name))
            if(self._emulator._method.lower() == 'regression'):
                print("{0: <{1}}\t{2}".format("Emulation method", width,
                                              "Regression"))
            elif(self._emulator._method.lower() == 'gaussian'):
                print("{0: <{1}}\t{2}".format("Emulation method", width,
                                              "Gaussian"))
            elif(self._emulator._method.lower() == 'full'):
                print("{0: <{1}}\t{2}".format("Emulation method", width,
                                              "Regression + Gaussian"))
            print("{0: <{1}}\t{2}".format("Mock data used?", width,
                                          "Yes" if self._emulator._use_mock
                                          else "No"))

            # ITERATION DETAILS
            print("\nITERATION")
            print("-"*width)

            # Emulator iteration corresponding to this details overview
            print("{0: <{1}}\t{2}".format("Emulator iteration", width, emul_i))

            # Availability flags
            # If this iteration is fully constructed, print flags and numbers
            if not len(self._emulator._ccheck[emul_i]):
                print("{0: <{1}}\t{2}".format("Construction completed?", width,
                                              "Yes"))
                if not self._n_eval_sam[emul_i]:
                    print("{0: <{1}}\t{2}".format("Plausible regions?", width,
                                                  "N/A"))
                else:
                    print("{0: <{1}}\t{2}".format(
                        "Plausible regions?", width,
                        "Yes" if self._prc else "No"))
                if not proj:
                    print("{0: <{1}}\t{2}".format("Projections available?",
                                                  width, "No"))
                else:
                    print("{0: <{1}}\t{2} ({3}/{4})".format(
                        "Projections available?", width,
                        "Yes%s" % ("" if proj == 1 else ", desynced"),
                        n_proj, n_proj_max))
                print("-"*width)

                # Number details
                if(self._emulator._emul_type == 'default'):
                    print("{0: <{1}}\t{2} ({3})".format(
                        "# of model evaluation samples", width,
                        sum(self._emulator._n_sam[1:emul_i+1]),
                        self._emulator._n_sam[1:emul_i+1]))
                else:
                    raise NotImplementedError
                if not self._n_eval_sam[emul_i]:
                    print("{0: <{1}}\t{2}/{3}".format(
                        "# of plausible/analyzed samples", width, "-", "-"))
                    print("{0: <{1}}\t{2}".format(
                        "% of parameter space remaining", width, "-"))
                else:
                    print("{0: <{1}}\t{2}/{3}".format(
                        "# of plausible/analyzed samples", width,
                        self._n_impl_sam[emul_i], self._n_eval_sam[emul_i]))
                    print("{0: <{1}}\t{2:.3g}%".format(
                        "% of parameter space remaining", width,
                        (self._n_impl_sam[emul_i] /
                         self._n_eval_sam[emul_i])*100))
                print("{0: <{1}}\t{2}/{3}".format(
                    "# of active/total parameters", width,
                    n_active_par, n_par))
                print("{0: <{1}}\t{2}".format("# of emulated data points",
                                              width,
                                              self._emulator._n_data[emul_i]))

            # If not, then print which components are still missing
            else:
                ccheck = self._emulator._ccheck[emul_i]
                print("{0: <{1}}\t{2}".format("Construction completed?", width,
                                              "No"))
                print("  - {0: <{1}}\t{2}".format(
                    "'mod_real_set'?", width-4,
                    "No" if 'mod_real_set' in ccheck else "Yes"))
                print("  - {0: <{1}}\t{2}".format(
                    "'active_par'?", width-4,
                    "No" if 'active_par' in ccheck else "Yes"))
                if self._emulator._method.lower() in ('regression', 'full'):
                    print("  - {0: <{1}}\t{2}".format(
                        "'regression'?", width-4,
                        "No" if 'regression' in ccheck else "Yes"))
                print("  - {0: <{1}}\t{2}".format(
                    "'prior_exp_sam_set'?", width-4,
                    "No" if 'prior_exp_sam_set' in ccheck else "Yes"))
                print("  - {0: <{1}}\t{2}".format(
                    "'cov_mat'?", width-4,
                    "No" if 'cov_mat' in ccheck else "Yes"))
            print("-"*width)

            # PARAMETER SPACE
            print("\nPARAMETER SPACE")
            print("-"*width)

            # Define string format if par_est was provided
            str_format1 = "{8}{0: <{1}}: [{2: >{3}}, {4: >{5}}] ({6: >{7}.5f})"

            # Define string format if par_est was not provided
            str_format2 = "{8}{0: <{1}}: [{2: >{3}}, {4: >{5}}] ({6:->{7}})"

            # Print details about every model parameter in parameter space
            for i in range(n_par):
                # Determine what string to use for the active flag
                if len(self._emulator._ccheck[emul_i]):
                    active_str = "?"
                elif i in self._emulator._active_par[emul_i]:
                    active_str = "*"
                else:
                    active_str = " "

                # Check if par_est is given and use correct string formatting
                if self._modellink._par_est[i] is not None:
                    print(str_format1.format(
                        self._modellink._par_name[i], name_len,
                        self._modellink._par_rng[i, 0], lower_len,
                        self._modellink._par_rng[i, 1], upper_len,
                        self._modellink._par_est[i], est_len, active_str))
                else:
                    print(str_format2.format(
                        self._modellink._par_name[i], name_len,
                        self._modellink._par_rng[i, 0], lower_len,
                        self._modellink._par_rng[i, 1], upper_len,
                        "", est_len, active_str))

            # FOOTER
            print("="*width)

        # MPI Barrier
        MPI.COMM_WORLD.Barrier()

    # This function allows the user to evaluate a given sam_set in the emulator
    # TODO: Plot emul_i_stop for large LHDs, giving a nice mental statistic
    @docstring_substitute(emul_i=user_emul_i_doc)
    def evaluate(self, sam_set, emul_i=None):
        """
        Evaluates the given model parameter sample set `sam_set` at given
        emulator iteration `emul_i`.
        The output of this function depends on the number of dimensions in
        `sam_set`.

        Parameters
        ----------
        sam_set : 1D or 2D array_like
            Array containing model parameter value sets to be evaluated in the
            emulator system.

        Optional
        --------
        %(emul_i)s

        Returns (if ndim(sam_set) > 1)
        ------------------------------
        impl_check : list of bool
            List of bool indicating whether or not the given samples passed the
            implausibility check at the given emulator iteration `emul_i`.
        emul_i_stop : list of int
            List containing the last emulator iteration identifiers at which
            the given samples are still within the emulator system.
        adj_exp_val : list of 1D :obj:`~numpy.ndarray` objects
            List of arrays containing the adjusted expectation values for all
            given samples.
        adj_var_val : list of 1D :obj:`~numpy.ndarray` objects
            List of arrays containing the adjusted variance values for all
            given samples.
        uni_impl_val : list of 1D :obj:`~numpy.ndarray` objects
            List of arrays containing the univariate implausibility values for
            all given samples.

        Prints (if ndim(sam_set) == 1)
        ------------------------------
        impl_check : bool
            Bool indicating whether or not the given sample passed the
            implausibility check at the given emulator iteration `emul_i`.
        emul_i_stop : int
            Last emulator iteration identifier at which the given sample is
            still within the emulator system.
        adj_exp_val : 1D :obj:`~numpy.ndarray` object
            The adjusted expectation values for the given sample.
        adj_var_val : 1D :obj:`~numpy.ndarray` object
            The adjusted variance values for the given sample.
        sigma_val : 1D :obj:`~numpy.ndarray` object
            The corresponding sigma value for the given sample.
        uni_impl_val : 1D :obj:`~numpy.ndarray` object
            The univariate implausibility values for the given sample.

        Notes
        -----
        If given emulator iteration `emul_i` has been analyzed before, the
        implausibility parameters of the last analysis are used. If not, then
        the values are used that were read in when the emulator system was
        loaded.

        """

        # Only controller
        if self._is_controller:
            # Do some logging
            logger = logging.getLogger('EVALUATE')
            logger.info("Evaluating emulator system for provided set of model "
                        "parameter samples.")

            # Get emulator iteration
            emul_i = self._emulator._get_emul_i(emul_i)

            # Make sure that sam_set is a NumPy array
            sam_set = np.array(sam_set)

            # Check the number of dimensions in sam_set
            if(sam_set.ndim == 1):
                print_output = 1
                sam_set = np.array(sam_set, ndmin=2)
            elif(sam_set.ndim == 2):
                print_output = 0
            else:
                logger.error("Input argument 'sam_set' is not one-dimensional "
                             "or two-dimensional!")
                raise ShapeError("Input argument 'sam_set' is not "
                                 "one-dimensional or two-dimensional!")

            # Check if sam_set has n_par parameter values
            if not(sam_set.shape[1] == self._modellink._n_par):
                logger.error("Input argument 'sam_set' has incorrect number of"
                             " parameters (%s != %s)!"
                             % (sam_set.shape[1], self._modellink._n_par))
                raise ShapeError("Input argument 'sam_set' has incorrect "
                                 "number of parameters (%s != %s)!"
                                 % (sam_set.shape[1], self._modellink._n_par))

            # Check if sam_set consists only out of floats (or ints)
            else:
                for i, par_set in enumerate(sam_set):
                    for j, par_val in enumerate(par_set):
                        check_float(par_val, 'sam_set[%s, %s]' % (i, j))

            # Make empty lists
            adj_exp_val = []
            adj_var_val = []
            uni_impl_val = []
            emul_i_stop = []
            impl_check = []

            # Iterate over all emulator iterations
            for par_set in sam_set:
                for j in range(1, emul_i+1):
                    # Obtain implausibility
                    adj_val = self._emulator._evaluate(j, par_set)
                    uni_impl_val_par_set = self._get_uni_impl(j, *adj_val)

                    # Check if this sample is plausible
                    if not self._do_impl_check(self, j,
                                               uni_impl_val_par_set)[0]:
                        impl_check.append(0)
                        break
                else:
                    impl_check.append(1)

                # Save expectation, variance and implausibility values
                adj_exp_val.append(adj_val[0])
                adj_var_val.append(adj_val[1])
                uni_impl_val.append(uni_impl_val_par_set)
                emul_i_stop.append(j)

            # Do more logging
            logger.info("Finished evaluating emulator system.")

            # If ndim(sam_set) == 1, print the results
            if print_output:
                # Print results
                if impl_check[0]:
                    print("Plausible? Yes")
                    print("-"*14)
                else:
                    print("Plausible? No")
                    print("-"*13)
                print("emul_i_stop = %s" % (emul_i_stop[0]))
                print("adj_exp_val = %s" % (adj_exp_val[0]))
                print("adj_var_val = %s" % (adj_var_val[0]))
                print("sigma_val = %s" % (np.sqrt(adj_var_val[0])))
                print("uni_impl_val = %s" % (uni_impl_val[0]))

            # Else, return the lists
            else:
                # MPI Barrier for controller
                MPI.COMM_WORLD.Barrier()

                # Return results
                return(impl_check, emul_i_stop, adj_exp_val, adj_var_val,
                       uni_impl_val)

        # MPI Barrier
        MPI.COMM_WORLD.Barrier()

    # TODO: Deprecated
    @docstring_copy(Projection.__call__)
    def create_projection(self, *args, **kwargs):
        # Print warning that this name is deprecated
        if self._is_controller:
            warnings.warn("This method has been renamed to 'project()' in "
                          "v0.4.22 and will be removed in v0.5.0!",
                          FutureWarning, stacklevel=2)

        # Call project()
        self.project(*args, **kwargs)

    # This function creates the projection figures of a given emul_i
    @docstring_copy(Projection.__call__)
    def project(self, emul_i=None, proj_par=None, figure=True, show=False,
                force=False):

        # Only controller
        if self._is_controller:
            # Initialize the Projection class and make the figures
            Projection(self)(emul_i, proj_par, figure, show, force)

        # MPI Barrier
        MPI.COMM_WORLD.Barrier()
