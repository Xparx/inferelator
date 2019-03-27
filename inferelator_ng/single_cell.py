import pandas as pd
import numpy as np

from inferelator_ng.default import DEFAULT_METADATA_FOR_BATCH_CORRECTION
from inferelator_ng.default import DEFAULT_RANDOM_SEED
from inferelator_ng import utils

"""
This file is all preprocessing functions. All functions must take positional arguments expression_matrix and meta_data.
All other arguments must be keyword. All functions must return expression_matrix and meta_data (modified or unmodified).

Normalization functions take batch_factor_column [str] as a kwarg
Imputation functions take random_seed [int] and output_file [str] as a kwarg 

Please note that there are a bunch of packages in here that aren't installed as part of the project dependencies
This is intentional; if you don't have these packages installed, don't try to use them
TODO: Put together a set of tests for this 
"""


def normalize_expression_to_one(expression_matrix, meta_data, **kwargs):
    """

    :param expression_matrix:
    :param meta_data:
    :param batch_factor_column:
    :return expression_matrix, meta_data: pd.DataFrame, pd.DataFrame
    """
    kwargs, batch_factor_column = process_normalize_args(**kwargs)

    utils.Debug.vprint('Normalizing UMI counts per cell ... ')

    # Get UMI counts for each cell
    umi = expression_matrix.sum(axis=1)

    # Divide each cell's raw count data by the total number of UMI counts for that cell
    return expression_matrix.astype(float).divide(umi, axis=0), meta_data


def normalize_medians_for_batch(expression_matrix, meta_data, **kwargs):
    """
    Calculate the median UMI count per cell for each batch. Transform all batches by dividing by a size correction
    factor, so that all batches have the same median UMI count (which is the median batch median UMI count)
    :param expression_matrix: pd.DataFrame
    :param meta_data: pd.DataFrame
    :param batch_factor_column: str
        Which meta data column should be used to determine batches
    :return expression_matrix, meta_data: pd.DataFrame, pd.DataFrame
    """
    kwargs, batch_factor_column = process_normalize_args(**kwargs)

    utils.Debug.vprint('Normalizing median counts between batches ... ')

    # Get UMI counts for each cell
    umi = expression_matrix.sum(axis=1)

    # Create a new dataframe with the UMI counts and the factor to batch correct on
    umi = pd.DataFrame({'umi': umi, batch_factor_column: meta_data[batch_factor_column]})

    # Group and take the median UMI count for each batch
    median_umi = umi.groupby(batch_factor_column).agg('median')

    # Convert to a correction factor based on the median of the medians
    median_umi = median_umi / median_umi['umi'].median()
    umi = umi.join(median_umi, on=batch_factor_column, how="left", rsuffix="_mod")

    # Apply the correction factor to all the data
    return expression_matrix.divide(umi['umi_mod'], axis=0), meta_data


def normalize_sizes_within_batch(expression_matrix, meta_data, **kwargs):
    """
    Calculate the median UMI count within each batch and then resize each sample so that each sample has the same total
    UMI count

    :param expression_matrix: pd.DataFrame
    :param meta_data: pd.DataFrame
    :param batch_factor_column: str
        Which meta data column should be used to determine batches
    :return expression_matrix, meta_data: pd.DataFrame, pd.DataFrame
    """

    kwargs, batch_factor_column = process_normalize_args(**kwargs)

    utils.Debug.vprint('Normalizing to median counts within batches ... ')

    # Get UMI counts for each cell
    umi = expression_matrix.sum(axis=1)

    # Create a new dataframe with the UMI counts and the factor to batch correct on
    umi = pd.DataFrame({'umi': umi, batch_factor_column: meta_data[batch_factor_column]})

    # Group and take the median UMI count for each batch
    median_umi = umi.groupby(batch_factor_column).agg('median')

    # Convert to a correction factor based on the median of the medians
    umi = umi.join(median_umi, on="Condition", how="left", rsuffix="_mod")
    umi['umi_mod'] = umi['umi'] / umi['umi_mod']

    # Apply the correction factor to all the data
    return expression_matrix.divide(umi['umi_mod'], axis=0), meta_data


def impute_magic_expression(expression_matrix, meta_data, **kwargs):
    """
    Use MAGIC (van Dijk et al Cell, 2018, 10.1016/j.cell.2018.05.061) to impute data

    :param expression_matrix: pd.DataFrame
    :param meta_data: pd.DataFrame
    :return imputed, meta_data: pd.DataFrame, pd.DataFrame
    """
    kwargs, random_seed, output_file = process_impute_args(**kwargs)

    import magic
    utils.Debug.vprint('Imputing data with MAGIC ... ')
    imputed = pd.DataFrame(magic.MAGIC(random_state=random_seed, **kwargs).fit_transform(expression_matrix.values),
                           index=expression_matrix.index, columns=expression_matrix.columns)

    if output_file is not None:
        imputed.to_csv(output_file, sep="\t")

    return imputed, meta_data


def log10_data(expression_matrix, meta_data, **kwargs):
    """
    Transform the expression data by adding one and then taking log10. Ignore any kwargs.
    :param expression_matrix: pd.DataFrame
    :param meta_data: pd.DataFrame
    :return expression_matrix, meta_data: pd.DataFrame, pd.DataFrame
    """
    utils.Debug.vprint('Logging data [log10+1] ... ')
    return np.log10(expression_matrix + 1), meta_data


def log2_data(expression_matrix, meta_data, **kwargs):
    """
    Transform the expression data by adding one and then taking log2. Ignore any kwargs.
    :param expression_matrix: pd.DataFrame
    :param meta_data: pd.DataFrame
    :return expression_matrix, meta_data: pd.DataFrame, pd.DataFrame
    """
    utils.Debug.vprint('Logging data [log2+1]... ')
    return np.log2(expression_matrix + 1), meta_data


def ln_data(expression_matrix, meta_data, **kwargs):
    """
    Transform the expression data by adding one and then taking ln. Ignore any kwargs.
    :param expression_matrix: pd.DataFrame
    :param meta_data: pd.DataFrame
    :return expression_matrix, meta_data: pd.DataFrame, pd.DataFrame
    """
    utils.Debug.vprint('Logging data [ln+1]... ')
    return np.log1p(expression_matrix), meta_data


def filter_genes_for_var(expression_matrix, meta_data, **kwargs):
    no_signal = (expression_matrix.max(axis=0) - expression_matrix.min(axis=0)) == 0
    utils.Debug.vprint("Filtering {gn} genes [Var = 0]".format(gn=no_signal.sum()), level=1)
    return expression_matrix.loc[:, ~no_signal], meta_data


def filter_genes_for_count(expression_matrix, meta_data, count_minimum=None, check_for_scaling=False):
    expression_matrix, meta_data = filter_genes_for_var(expression_matrix, meta_data)
    if count_minimum is None:
        return expression_matrix, meta_data
    else:
        if check_for_scaling and (expression_matrix < 0).sum().sum() > 0:
            raise ValueError("Negative values in the expression matrix. Count thresholding scaled data is unsupported.")

        keep_genes = expression_matrix.sum(axis=0) >= (count_minimum * expression_matrix.shape[0])
        utils.Debug.vprint("Filtering {gn} genes [Count]".format(gn=expression_matrix.shape[1] - keep_genes.sum()),
                           level=1)
        return expression_matrix.loc[:, keep_genes], meta_data


def process_impute_args(**kwargs):
    random_seed = kwargs.pop('random_seed', DEFAULT_RANDOM_SEED)
    output_file = kwargs.pop('output_file', None)
    return kwargs, random_seed, output_file


def process_normalize_args(**kwargs):
    batch_factor_column = kwargs.pop('batch_factor_column', DEFAULT_METADATA_FOR_BATCH_CORRECTION)
    return kwargs, batch_factor_column
