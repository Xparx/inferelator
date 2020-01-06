from __future__ import print_function, unicode_literals, division

import copy
import pandas as pd
import numpy as np
import scipy.sparse as sparse
import pandas.api.types as pat
from anndata import AnnData
from inferelator.utils.debug import Debug


def df_from_tsv(file_like, has_index=True):
    "Read a tsv file or buffer with headers and row ids into a pandas dataframe."
    return pd.read_csv(file_like, sep="\t", header=0, index_col=0 if has_index else False)


def df_set_diag(df, val, copy=True):
    """
    Sets the diagonal of a dataframe to a value. Diagonal in this case is anything where row label == column label.

    :param df: pd.DataFrame
        DataFrame to modify
    :param val: numeric
        Value to insert into any cells where row label == column label
    :param copy: bool
        Force-copy the dataframe instead of modifying in place
    :return: pd.DataFrame / int
        Return either the modified dataframe (if copied) or the number of cells modified (if changed in-place)
    """

    # Find all the labels that are shared between rows and columns
    isect = df.index.intersection(df.columns)

    if copy:
        df = df.copy()

    # Set the value where row and column names are the same
    for i in range(len(isect)):
        df.loc[isect[i], isect[i]] = val

    if copy:
        return df
    else:
        return len(isect)


def make_array_2d(arr):
    """
    Changes array shape from 1d to 2d if needed (in-place)
    :param arr:  np.ndarray
    """
    if arr.ndim == 1:
        arr.shape = (arr.shape[0], 1)


def melt_and_reindex_dataframe(data_frame, value_name, idx_name="target", col_name="regulator"):
    """
    Take a pandas dataframe and melt it into a one column dataframe (with the column `value_name`) and a multiindex
    of the original index + column
    :param data_frame: pd.DataFrame [M x N]
        Meltable dataframe
    :param value_name: str
        The column name for the values of the dataframe
    :param idx_name: str
        The name to assign to the original data_frame index values
    :param col_name: str
        The name to assign to the original data_frame column values
    :return: pd.DataFrame [(M*N) x 1]
        Melted dataframe with a single column of values and a multiindex that is the original index + column for
        that value
    """

    # Copy the dataframe and move the index to a column
    data_frame = data_frame.copy()
    data_frame[idx_name] = data_frame.index

    # Melt it into a [(M*N) x 3] dataframe
    data_frame = data_frame.melt(id_vars=idx_name, var_name=col_name, value_name=value_name)

    # Create a multiindex and then drop the columns that are now in the index
    data_frame.index = pd.MultiIndex.from_frame(data_frame.loc[:, [idx_name, col_name]])
    del data_frame[idx_name]
    del data_frame[col_name]

    return data_frame


def is_sparse_series(series):
    """
    Test a pandas Series (column) to see if it is sparse
    :param series: pd.Series
    :return is_sparse: bool
    """
    return isinstance(series.dtype, pd.SparseDtype)


def transpose_dataframe(data_frame):
    """
    Take a pandas dataframe and transpose it. This is a wrapper which does sparse transposition for a sparse matrix.
    :param data_frame: pd.DataFrame
    :return data_frame: pd.DataFrame
    """
    is_all_sparse = all([is_sparse_series(data_frame.iloc[:, dt]) for dt in range(data_frame.shape[1])])

    if is_all_sparse:
        return pd.DataFrame.sparse.from_spmatrix(data_frame.sparse.to_coo().transpose(),
                                                 index=data_frame.columns,
                                                 columns=data_frame.index)
    else:
        return data_frame.transpose()


class InferelatorData(object):
    """ Store inferelator data in an AnnData object. This will always be Samples by Genes """

    _adata = None
    _is_integer = False

    @property
    def expression_data(self):
        return self._adata.X

    @property
    def _data(self):
        if self.is_sparse:
            return self._adata.X.data
        else:
            return self._adata.X

    @_data.setter
    def _data(self, new_data):
        if self.is_sparse:
            self._adata.X.data = new_data
        else:
            self._adata.X = new_data

    @property
    def meta_data(self):
        return self._adata.obs

    @meta_data.setter
    def meta_data(self, new_meta_data):
        # Reindex the new metadata to match the existing sample names
        new_meta_data = new_meta_data.copy()
        new_meta_data.index = new_meta_data.index.astype(str)
        new_meta_data = new_meta_data.reindex(self.sample_names)

        # Join any new columns to any existing columns
        # Update (overwrite) any columns in the existing meta data if they are in the new meta data
        if len(self._adata.obs.columns) > 0:
            keep_columns = self._adata.obs.columns.difference(new_meta_data.columns)
            self._adata.obs = pd.concat((new_meta_data, self._adata.obs.loc[:, keep_columns]))
        else:
            self._adata.obs = new_meta_data

    @property
    def gene_data(self):
        return self._adata.var

    @gene_data.setter
    def gene_data(self, new_gene_data):

        new_gene_data = new_gene_data.copy()
        new_gene_data.index = new_gene_data.index.astype(str)

        # Use the intersection of this and the expression data genes to make a list of gene names to keep
        self._adata.uns["trim_gene_list"] = new_gene_data.index.intersection(self._adata.var.index)

        new_gene_data = new_gene_data.reindex(self._adata.var_names)

        # Join any new columns to any existing columns
        # Update (overwrite) any columns in the existing meta data if they are in the new meta data
        if len(self._adata.var.columns) > 0:
            keep_columns = self._adata.var.columns.difference(new_gene_data.columns)
            self._adata.var = pd.concat((new_gene_data, self._adata.var.loc[:, keep_columns]))
        else:
            self._adata.var = new_gene_data

    @property
    def gene_names(self):
        return self._adata.var_names

    @property
    def gene_counts(self):
        return self._adata.X.sum(axis=0).A.flatten() if self.is_sparse else self._adata.X.sum(axis=0)

    @property
    def sample_names(self):
        return self._adata.obs_names

    @property
    def sample_counts(self):
        return self._adata.X.sum(axis=1).A.flatten() if self.is_sparse else self._adata.X.sum(axis=1)

    @property
    def non_finite(self):
        if min(self._data.shape) == 0:
            return 0, None
        elif self.is_sparse and sparse.isspmatrix_csr(self._adata.X):
            nan_indices = np.unique(self._adata.X.indices[~np.isfinite(self._adata.X.data)])
            nnf = nan_indices.shape[0]
            return nnf, self.gene_names[nan_indices] if nnf > 0 else None
        elif self.is_sparse and sparse.isspmatrix_csc(self._adata.X):
            nan_indices = sparse.csc_matrix((~np.isfinite(self._adata.X.data),
                                             self._adata.X.indices,
                                             self._adata.X.indptr),
                                            shape=self._adata.shape, dtype=bool).A.sum(axis=0) > 0
            nnf = np.sum(nan_indices)
            return nnf, self.gene_names[nan_indices.flatten()] if nnf > 0 else None
        else:
            non_finite = np.apply_along_axis(lambda x: np.sum(~np.isfinite(x)) > 0, 0, self._data)
            nnf = np.sum(non_finite)
            return nnf, self.gene_names[non_finite] if nnf > 0 else None

    @property
    def is_sparse(self):
        return sparse.issparse(self._adata.X)

    @property
    def shape(self):
        return self._adata.shape

    @property
    def num_obs(self):
        return self._adata.shape[0]

    @property
    def num_genes(self):
        return self._adata.shape[1]

    def __init__(self, expression_data, transpose_expression=False, meta_data=None, gene_data=None, gene_names=None,
                 sample_names=None, dtype=None):

        if isinstance(expression_data, pd.DataFrame):
            object_cols = expression_data.dtypes == object

            if sum(object_cols) > 0:
                object_data = expression_data.loc[:, object_cols]
                meta_data = object_data if meta_data is None else pd.concat((meta_data, object_data))
                expression_data.drop(expression_data.columns[object_cols], inplace=True, axis=1)

            if dtype is None and all(map(lambda x: pat.is_integer_dtype(x), expression_data.dtypes)):
                dtype = 'int32'
            elif dtype is None:
                dtype = 'float64'

            self._is_integer = pat.is_integer_dtype(dtype)
            self._make_idx_str(expression_data)

            if transpose_expression:
                self._adata = AnnData(X=expression_data.T, dtype=dtype)
            else:
                self._adata = AnnData(X=expression_data, dtype=dtype)
        else:
            if transpose_expression:
                self._adata = AnnData(X=expression_data.T, dtype=expression_data.dtype)
            else:
                self._adata = AnnData(X=expression_data, dtype=expression_data.dtype)

            self._is_integer = True if pat.is_integer_dtype(expression_data.dtype) else False

        if gene_names is not None:
            self._adata.var_names = gene_names

        if sample_names is not None:
            self._adata.obs_names = sample_names

        if meta_data is not None:
            self._make_idx_str(meta_data)
            self.meta_data = meta_data

        if gene_data is not None:
            self._make_idx_str(gene_data)
            self.gene_data = gene_data

    def convert_to_float(self):
        if pat.is_float_dtype(self._data.dtype):
            return None
        elif self._data.dtype == np.int32:
            dtype = np.float32
        elif self._data.dtype == np.int64:
            dtype = np.float64
        else:
            raise ValueError("Data is not float, int32, or int64")

        float_view = self._data.view(dtype)
        float_view[:] = self._data
        self._data = float_view

        self._is_integer = False

    def trim_genes(self, remove_constant_genes=True, trim_gene_list=None):
        """
        Remove genes (columns) that are unwanted from the data set
        :param remove_constant_genes:
        :type remove_constant_genes: bool
        :param trim_gene_list:
        :return:
        """

        if trim_gene_list is not None:
            keep_column_bool = self._adata.var.index.isin(trim_gene_list)
        elif "trim_gene_list" in self._adata.uns:
            keep_column_bool = self._adata.var.index.isin(self._adata.uns["trim_gene_list"])
        else:
            keep_column_bool = np.ones((len(self._adata.var.index),), dtype=bool)

        list_trim = len(self._adata.var.index) - np.sum(keep_column_bool)
        comp = 0 if self._is_integer else np.finfo(self.expression_data.dtype).eps * 10

        if remove_constant_genes:
            if self.is_sparse:
                keep_column_bool &= self.expression_data.getnnz(axis=0) > 0
                nz_var = self.expression_data.min(axis=0).A.flatten() != self.expression_data.max(axis=0).A.flatten()
                keep_column_bool &= nz_var
            else:
                keep_column_bool &= np.apply_along_axis(lambda x: np.max(x) - np.min(x), 0, self.expression_data) > comp

            var_zero_trim = len(self._adata.var.index) - np.sum(keep_column_bool) + list_trim
        else:
            var_zero_trim = 0

        if np.sum(keep_column_bool) == 0:
            err_msg = "No genes remain after trimming. ({lst} removed to match list, {v} removed for var=0)"
            raise ValueError(err_msg.format(lst=list_trim, v=var_zero_trim))

        if np.sum(keep_column_bool) == self._adata.shape[1]:
            pass
        else:
            # This explicit copy allows the original to be deallocated
            # Otherwise the GC leaves the original because the view reference keeps it alive
            # At some point it will need to copy so why not now
            self._adata = self._adata[:, keep_column_bool].copy()

    def get_genes(self, gene_list, copy=False):

        return self._adata[:, gene_list].X if not copy else self._adata[:, gene_list].X.copy()

    def dot(self, other, other_is_right_side=True, force_dense=False):
        """
        Calculate dot product
        :param other:
        :param other_is_right_side:
        :param force_dense:
        :return:
        """

        # If both are sparse use scipy.dot() and make a sparse product
        if self.is_sparse and sparse.issparse(other):
            dot_product = self._adata.X.dot(other) if other_is_right_side else other.dot(self._adata.X)

        # If this data struture is sparse, convert the other to sparse and use scipy.dot() and make a sparse product
        elif self.is_sparse and not sparse.issparse(other):
            other = sparse.csr_matrix(other)
            dot_product = self._adata.X.dot(other) if other_is_right_side else other.dot(self._adata.X)

        # If this data structure is dense, convert the other to dense and use np.dot() and make a dense product
        elif not self.is_sparse and sparse.issparse(other):
            dot_product = np.dot(self._adata.X, other.A) if other_is_right_side else np.dot(other.A, self._adata.X)

        # If both data structures are dense, use np.dot() and make a dense product
        else:
            dot_product = np.dot(self._adata.X, other) if other_is_right_side else np.dot(other, self._adata.X)

        # Convert a sparse product to dense if force_dense is set
        return dot_product.A if force_dense and sparse.issparse(dot_product) else dot_product

    def to_csv(self, file_name, sep="\t"):

        if self.is_sparse:
            Debug.vprint("Saving sparse arrays to text files is not supported", level=0)
        else:
            np.savetxt(file_name, self.expression_data, delimiter=sep, header=sep.join(self.gene_names))

    def transform(self, func, add_pseudocount=False, memory_efficient=True, chunksize=1000):

        if add_pseudocount and self.is_sparse:
            self._adata.X.data += 1
        elif add_pseudocount:
            self._adata.X += 1

        if self.is_sparse:
            self._adata.X.data = func(self._adata.X.data)
        elif self._adata.X.ndim == 1 or self._is_integer:
            self._adata.X = func(self._adata.X)
        elif not memory_efficient and type(func(self._adata.X[0, 0])) == self._adata.X.dtype:
            self._adata.X[...] = func(self._adata.X)
        elif memory_efficient and type(func(self._adata.X[0, 0])) == self._adata.X.dtype:
            for i in range(np.ceil(self._adata.shape[0] / chunksize)):
                start, stop = i * chunksize, min(i + 1 * chunksize, self._adata.shape[0])
                self._adata.X[start:stop, :] = func(self._adata.X[start:stop, :])
        else:
            self._adata.X = func(self._adata.X)

    def divide(self, div_val, axis=None):

        if self._is_integer:
            self.convert_to_float()

        if self.is_sparse and axis is None:
            self._adata.X.data /= div_val
        elif self.is_sparse and ((sparse.isspmatrix_csr(self._adata.X) and axis == 1) or
                                 (sparse.isspmatrix_csc(self._adata.X) and axis == 0)):
            if not hasattr(div_val, "ndim") or div_val.ndim != 1 or self.shape[0 if axis else 1] != div_val.shape[0]:
                raise ValueError("Division array is not aligned")
            self._adata.X.data /= np.repeat(div_val, self._adata.X.getnnz(axis=axis))
        elif self.is_sparse:
            raise ValueError("axis = 1 only works for CSC & axis = 0 only works for CSR")
        elif axis is None:
            self._adata.X /= div_val
        elif axis == 0:
            self._adata.X /= div_val[None, :]
        elif axis == 1:
            self._adata.X /= div_val[:, None]
        else:
            raise ValueError("axis must be 0, 1 or None")

    def multiply(self, mult_val, axis=None):

        if self._is_integer:
            self.convert_to_float()

        if self.is_sparse and axis is None:
            self._adata.X.data *= mult_val
        elif self.is_sparse and ((sparse.isspmatrix_csr(self._adata.X) and axis == 1) or
                                 (sparse.isspmatrix_csc(self._adata.X) and axis == 0)):
            if not hasattr(mult_val, "ndim") or mult_val.ndim != 1 or self.shape[0 if axis else 1] != mult_val.shape[0]:
                raise ValueError("Division array is not aligned")
            self._adata.X.data *= np.repeat(mult_val, self._adata.X.getnnz(axis=axis))
        elif self.is_sparse:
            raise ValueError("axis = 1 only works for CSC & axis = 0 only works for CSR")
        elif axis is None:
            self._adata.X *= mult_val
        elif axis == 0:
            self._adata.X *= mult_val[None, :]
        elif axis == 1:
            self._adata.X *= mult_val[:, None]
        else:
            raise ValueError("axis must be 0, 1 or None")

    def copy(self):

        new_data = InferelatorData(self.expression_data.copy(),
                                   meta_data=self.meta_data.copy(),
                                   gene_data=self.gene_data.copy())

        new_data._adata.var_names = copy.copy(self._adata.var_names)
        new_data._adata.obs_names = copy.copy(self._adata.obs_names)
        new_data._adata.uns = copy.copy(self._adata.uns)

        return new_data

    @staticmethod
    def _make_idx_str(df):
        df.index = df.index.astype(str) if not pat.is_string_dtype(df.index.dtype) else df.index
        df.columns = df.columns.astype(str) if not pat.is_string_dtype(df.columns.dtype) else df.columns