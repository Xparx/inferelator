"""
Base implementation for high level workflow.

The goal of this design is to make it easy to share
code among different variants of the Inferelator workflow.
"""
from __future__ import unicode_literals, print_function

from inferelator import utils
from inferelator.utils import Validator as check
from inferelator import default
from inferelator.preprocessing.prior_gs_split_workflow import split_for_cv, remove_prior_circularity
from inferelator.regression.base_regression import RegressionWorkflow

from inferelator.distributed.inferelator_mp import MPControl

import inspect
import numpy as np
import os
import datetime
import pandas as pd

# Python 2/3 compatible string checking
try:
    basestring
except NameError:
    basestring = str


class WorkflowBase(object):
    # Paths to the input and output locations
    input_dir = None
    output_dir = None

    # Settings that will be used by pd.read_table to import data files
    file_format_settings = default.DEFAULT_PD_INPUT_SETTINGS
    # A dict, keyed by file name, of settings to override the defaults in file_format_settings
    # Used when input files are perhaps not processed into perfect TSVs
    file_format_overrides = dict()

    # File names for each of the data files which can be used in the inference workflow
    expression_matrix_file = default.DEFAULT_EXPRESSION_FILE
    tf_names_file = default.DEFAULT_TFNAMES_FILE
    meta_data_file = default.DEFAULT_METADATA_FILE
    priors_file = default.DEFAULT_PRIORS_FILE
    gold_standard_file = default.DEFAULT_GOLDSTANDARD_FILE

    # The random seed for sampling, etc
    random_seed = default.DEFAULT_RANDOM_SEED

    # The number of inference bootstraps to run
    num_bootstraps = default.DEFAULT_NUM_BOOTSTRAPS

    # Flags to control splitting priors into a prior/gold-standard set
    split_priors_for_gold_standard = False
    split_gold_standard_for_crossvalidation = False
    cv_split_ratio = default.DEFAULT_GS_SPLIT_RATIO
    cv_split_axis = default.DEFAULT_GS_SPLIT_AXIS
    shuffle_prior_axis = None

    # Computed data structures [G: Genes, K: Predictors, N: Conditions
    expression_matrix = None  # expression_matrix dataframe [G x N]
    tf_names = None  # tf_names list [k,]
    meta_data = None  # meta data dataframe [G x ?]
    priors_data = None  # priors data dataframe [G x K]
    gold_standard = None  # gold standard dataframe [G x K]

    # Multiprocessing controller
    initialize_mp = True
    multiprocessing_controller = None

    def __init__(self):
        # Get environment variables
        self.get_environmentals()

    def initialize_multiprocessing(self):
        """
        Register the multiprocessing controller if set and run .connect()
        """
        if self.multiprocessing_controller is not None:
            MPControl.set_multiprocess_engine(self.multiprocessing_controller)
        MPControl.connect()

    def get_environmentals(self):
        """
        Load environmental variables into class variables
        """
        for k, v in utils.slurm_envs(default.SBATCH_VARS_FOR_WORKFLOW).items():
            setattr(self, k, v)

    def startup(self):
        """
        Startup by preprocessing all data into a ready format for regression.
        """
        if self.initialize_mp:
            self.initialize_multiprocessing()
        self.startup_run()
        self.startup_finish()

    def startup_run(self):
        """
        Execute any data preprocessing necessary before regression. Startup_run is mostly for reading in data
        """
        raise NotImplementedError  # implement in subclass

    def startup_finish(self):
        """
        Execute any data preprocessing necessary before regression. Startup_finish is mostly for preprocessing data
        prior to regression
        """
        raise NotImplementedError  # implement in subclass

    def run(self):
        """
        Execute workflow, after all configuration.
        """
        raise NotImplementedError  # implement in subclass

    def get_data(self):
        """
        Read data files in to data structures.
        """

        self.read_expression()
        self.read_tfs()
        self.read_metadata()
        self.set_gold_standard_and_priors()

    def read_expression(self, file=None):
        """
        Read expression matrix file into expression_matrix
        """
        if file is None:
            file = self.expression_matrix_file
        self.expression_matrix = self.input_dataframe(file)

    def read_tfs(self, file=None):
        """
        Read tf names file into tf_names
        """

        # Load the class variable if no file is passed
        file = self.tf_names_file if file is None else file

        # Read in a dataframe with no header or index
        tfs = self.input_dataframe(file, header=None, index_col=None)

        # Cast the dataframe into a list
        assert tfs.shape[1] == 1
        self.tf_names = tfs.values.flatten().tolist()

    def read_metadata(self, file=None):
        """
        Read metadata file into meta_data or make fake metadata
        """
        if file is None:
            file = self.meta_data_file

        try:
            self.meta_data = self.input_dataframe(file, index_col=None)
        except IOError:
            self.meta_data = self.create_default_meta_data(self.expression_matrix)

    def set_gold_standard_and_priors(self):
        """
        Read priors file into priors_data and gold standard file into gold_standard
        """
        self.priors_data = self.input_dataframe(self.priors_file)

        if self.split_priors_for_gold_standard:
            self.split_priors_into_gold_standard()
        else:
            self.gold_standard = self.input_dataframe(self.gold_standard_file)

        if self.split_gold_standard_for_crossvalidation:
            self.cross_validate_gold_standard()

        try:
            check.index_values_unique(self.priors_data.index)
        except ValueError as v_err:
            utils.Debug.vprint("Duplicate gene(s) in prior index", level=0)
            utils.Debug.vprint(str(v_err), level=0)

        try:
            check.index_values_unique(self.priors_data.columns)
        except ValueError as v_err:
            utils.Debug.vprint("Duplicate tf(s) in prior index", level=0)
            utils.Debug.vprint(str(v_err), level=0)

    def split_priors_into_gold_standard(self):
        """
        Break priors_data in half and give half to the gold standard
        """

        if self.gold_standard is not None:
            utils.Debug.vprint("Existing gold standard is being replaced by a split from the prior", level=0)
        self.priors_data, self.gold_standard = split_for_cv(self.priors_data,
                                                            self.cv_split_ratio,
                                                            split_axis=self.cv_split_axis,
                                                            seed=self.random_seed)

        utils.Debug.vprint("Prior split into a prior {pr} and a gold standard {gs}".format(pr=self.priors_data.shape,
                                                                                           gs=self.gold_standard.shape),
                           level=0)

    def cross_validate_gold_standard(self):
        """
        Sample the gold standard for crossvalidation, and then remove the new gold standard from the priors
        """

        utils.Debug.vprint("Resampling prior {pr} and gold standard {gs}".format(pr=self.priors_data.shape,
                                                                                 gs=self.gold_standard.shape), level=0)
        _, self.gold_standard = split_for_cv(self.gold_standard,
                                             self.cv_split_ratio,
                                             split_axis=self.cv_split_axis,
                                             seed=self.random_seed)
        self.priors_data, self.gold_standard = remove_prior_circularity(self.priors_data, self.gold_standard,
                                                                        split_axis=self.cv_split_axis)
        utils.Debug.vprint("Selected prior {pr} and gold standard {gs}".format(pr=self.priors_data.shape,
                                                                               gs=self.gold_standard.shape), level=0)

    def shuffle_priors(self):
        """
        Shuffle prior labels if shuffle_prior_axis is set
        """

        if self.shuffle_prior_axis is None:
            return None
        elif self.shuffle_prior_axis == 0:
            # Shuffle index (genes) in the priors_data
            utils.Debug.vprint("Randomly shuffling prior [{sh}] gene data".format(sh=self.priors_data.shape))
            prior_index = self.priors_data.index.tolist()
            self.priors_data = self.priors_data.sample(frac=1, axis=0, random_state=self.random_seed)
            self.priors_data.index = prior_index
        elif self.shuffle_prior_axis == 1:
            # Shuffle columns (TFs) in the priors_data
            utils.Debug.vprint("Randomly shuffling prior [{sh}] TF data".format(sh=self.priors_data.shape))
            prior_index = self.priors_data.columns.tolist()
            self.priors_data = self.priors_data.sample(frac=1, axis=1, random_state=self.random_seed)
            self.priors_data.columns = prior_index
        else:
            raise ValueError("shuffle_prior_axis must be 0 or 1")

    def input_path(self, filename):
        """
        Join filename to input_dir
        """

        return os.path.abspath(os.path.expanduser(os.path.join(self.input_dir, filename)))

    def input_dataframe(self, filename, **kwargs):
        """
        Read a file in as a pandas dataframe
        """

        # Set defaults for index_col and header
        kwargs['index_col'] = kwargs.pop('index_col', 0)
        kwargs['header'] = kwargs.pop('header', 0)

        # Use any kwargs for this function and any file settings from default
        file_settings = self.file_format_settings.copy()
        file_settings.update(kwargs)

        # Update the file settings with anything that's in file-specific overrides
        if filename in self.file_format_overrides:
            file_settings.update(self.file_format_overrides[filename])

        # Load a dataframe
        return pd.read_csv(self.input_path(filename), **file_settings)

    def append_to_path(self, var_name, to_append):
        """
        Add a string to an existing path variable in class
        """
        path = getattr(self, var_name, None)
        if path is None:
            raise ValueError("Cannot append {to_append} to {var_name} (Which is None)".format(to_append=to_append,
                                                                                              var_name=var_name))
        setattr(self, var_name, os.path.join(path, to_append))

    @staticmethod
    def create_default_meta_data(expression_matrix):
        """
        Create a meta_data dataframe from basic defaults
        """
        metadata_rows = expression_matrix.columns.tolist()
        metadata_defaults = {"isTs": "FALSE", "is1stLast": "e", "prevCol": "NA", "del.t": "NA", "condName": None}
        data = {}
        for key in metadata_defaults.keys():
            data[key] = pd.Series(data=[metadata_defaults[key] if metadata_defaults[key] else i for i in metadata_rows])
        return pd.DataFrame(data)

    def filter_expression_and_priors(self):
        """
        Guarantee that each row of the prior is in the expression and vice versa.
        Also filter the priors to only includes columns, transcription factors, that are in the tf_names list
        """
        expressed_targets = self.expression_matrix.index
        expressed_or_prior = expressed_targets.union(self.priors_data.columns)
        keeper_regulators = expressed_or_prior.intersection(self.tf_names)

        if len(keeper_regulators) == 0 or len(expressed_targets) == 0:
            raise ValueError("Filtering will result in a priors with at least one axis of 0 length")

        self.priors_data = self.priors_data.reindex(expressed_targets, axis=0)
        self.priors_data = self.priors_data.reindex(keeper_regulators, axis=1)
        self.priors_data = pd.DataFrame.fillna(self.priors_data, 0)

        self.shuffle_priors()

    def get_bootstraps(self):
        """
        Generate sequence of bootstrap parameter objects for run.
        """
        col_range = range(self.response.shape[1])
        random_state = np.random.RandomState(seed=self.random_seed)
        return random_state.choice(col_range, size=(self.num_bootstraps, self.response.shape[1])).tolist()

    def emit_results(self, betas, rescaled_betas, gold_standard, priors):
        """
        Output result report(s) for workflow run.
        """
        raise NotImplementedError  # implement in subclass

    def is_master(self):
        """
        Return True if this is the master thread
        """
        return MPControl.is_master

    def create_output_dir(self):
        """
        Set a default output_dir if nothing is set. Create the path if it doesn't exist.
        """
        if self.output_dir is None:
            new_path = datetime.datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
            self.output_dir = os.path.expanduser(os.path.join(self.input_dir, new_path))
        try:
            os.makedirs(self.output_dir)
        except OSError:
            pass


def create_inferelator_workflow(regression=RegressionWorkflow, workflow=WorkflowBase):
    """
    This is the factory method to create workflow ckasses that combine preprocessing and postprocessing (from workflow)
    with a regression method (from regression)

    :param regression: RegressionWorkflow subclass
        A class object which implements the run_regression and run_bootstrap methods for a specific regression strategy
    :param workflow: WorkflowBase subclass
        A class object which implements the necessary data loading and preprocessing to create design & response data
        for the regression strategy, and then the postprocessing to turn regression betas into a network
    :return RegressWorkflow:
        This returns an uninstantiated class which is the multi-inheritance result of both the regression workflow and
        the preprocessing/postprocessing workflow
    """

    # Decide which preprocessing/postprocessing workflow to use
    # String arguments are parsed for convenience in the run script
    if isinstance(workflow, basestring):
        if workflow == "base":
            workflow_class = WorkflowBase
        elif workflow == "tfa":
            from inferelator.tfa_workflow import TFAWorkFlow
            workflow_class = TFAWorkFlow
        elif workflow == "amusr":
            from inferelator.amusr_workflow import SingleCellMultiTask
            workflow_class = SingleCellMultiTask
        elif workflow == "single-cell":
            from inferelator.single_cell_workflow import SingleCellWorkflow
            workflow_class = SingleCellWorkflow
        else:
            raise ValueError("{val} is not a string that can be mapped to a workflow class".format(val=workflow))
    # Or just use a workflow class directly
    elif inspect.isclass(workflow) and issubclass(workflow, WorkflowBase):
        workflow_class = workflow
    else:
        raise ValueError("Workflow must be a string that maps to a workflow class or an actual workflow class")

    # Decide which regression workflow to use
    # Return just the workflow if regression is set to None
    if regression is None:
        return workflow_class
    # String arguments are parsed for convenience in the run script
    elif isinstance(regression, basestring):
        if regression == "bbsr":
            from inferelator.regression.bbsr_python import BBSRRegressionWorkflow
            regression_class = BBSRRegressionWorkflow
        elif regression == "elasticnet":
            from inferelator.regression.elasticnet_python import ElasticNetWorkflow
            regression_class = ElasticNetWorkflow
        elif regression == "amusr":
            from inferelator.regression.amusr_regression import AMUSRRegressionWorkflow
            regression_class = AMUSRRegressionWorkflow
        else:
            raise ValueError("{val} is not a string that can be mapped to a regression class".format(val=regression))
    # Or just use a regression class directly
    elif inspect.isclass(regression) and issubclass(regression, RegressionWorkflow):
        regression_class = regression
    else:
        raise ValueError("Regression must be a string that maps to a regression class or an actual regression class")

    class RegressWorkflow(regression_class, workflow_class):
        regression_type = regression_class

    return RegressWorkflow


def inferelator_workflow(regression=RegressionWorkflow, workflow=WorkflowBase):
    """
    Create and instantiate a workflow

    :param regression: RegressionWorkflow subclass
        A class object which implements the run_regression and run_bootstrap methods for a specific regression strategy
    :param workflow: WorkflowBase subclass
        A class object which implements the necessary data loading and preprocessing to create design & response data
        for the regression strategy, and then the postprocessing to turn regression betas into a network
    :return RegressWorkflow:
        This returns an initialized object which is the multi-inheritance result of both the regression workflow and
        the preprocessing/postprocessing workflow
    """
    return create_inferelator_workflow(regression=regression, workflow=workflow)()