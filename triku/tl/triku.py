import scanpy as sc
import pandas as pd
import numpy as np
import scipy.sparse as spr

from ..genutils import get_cpu_count
from ._triku_functions import return_knn_indices, return_knn_expression, create_random_count_matrix, \
    parallel_emd_calculation, subtract_median, get_cutoff_curve
from ..utils._triku_tl_utils import return_mean, return_proportion_zeros, get_arr_counts_and_genes
from ..utils._general_utils import set_level_logger
from ..logg import triku_logger, TRIKU_LEVEL

import warnings
import logging

warnings.filterwarnings('ignore')  # To ignore Numba warnings


def triku(object_triku: [sc.AnnData, pd.DataFrame], n_features: [None, int] = None, use_raw=True,
          do_return: [None, bool] = None, use_adata_knn: [None, bool] = None,
          knn: [None, int] = None, s: [None, int, float] = -0.01, apply_background_correction: bool = True,
          n_comps: int = 25, metric: str = 'cosine', n_windows: int = 75, min_knn: int = 6,
          random_state: [None, int] = 0, n_procs: [None, int] = None, verbose: [None, str] = 'warning'):
    """
    This function calls the triku method using python directly. This function expects an
    annData object or a csv / txt matrix of n_cells x n_genes. The function then returns an array / list
    of the selected genes.

    Parameters
    ----------
    object_triku : scanpy.AnnData or pandas.DataFrame
        Object with count matrix. If `pandas.DataFrame`, rows are cells and columns are genes.
    n_features : int, None
        Number of features to select. If None, the number is chosen automatically.
    use_raw : bool
        If True, selects the adata.raw matrix, if it exists. This matrix is adjusted to select the genes and cells that
        appear in the current adata. E.g. if we are running triku with a subpopulation, triku will select the cells
        from adata.raw of that subpopulation. If certain genes have been removed after saving the raw, triku will not
        consider the removed genes.
    do_return : bool, None
        If True, returns a dictionary with # TODO: add what features it returns
    use_adata_knn :  bool, None
        If object_triku is a scanpy.AnnData object, and sc.pp.neighbors was run, select neighbors and knn from
        adata.uns['neighbors']['connectivities'] and  adata.uns['neighbors']['params']['n_neighbors'].
    knn: int, None
        If use_adata_knn is False, number of neighbors to choose for feature selection. By default
        the half the square root of the number of cells is chosen.
    s : float
        Correction factor for automatic feature selection. Negative values imply a selction of more genes, and
        positive values imply a selection of fewer genes. We recommend values between -0.1 and 0.1.
    apply_background_correction : bool
        Substract the Wasserstein distance from a randomised adata to compensate the inflation of Wasserstein distance
        of highly expressed genes. If the dataset is too big, this step can be ommited, since those features usually
        don't get selected.
    n_comps : int
        Number of PCA components for knn selection.
    metric : str
        Metric for knn selection.
    n_windows : int
        Number of windows used for median subtraction of EMD.
    min_knn : int
        minimum number of expressed cells based on the knn to apply thee convolution. If a genes has less than min_knn
        expressing cells, EMD is set to 0, and the convolution is set as the knn expression.
    random_state : int
        Seed for random processes
    n_procs : int, None
        Number of processes for parallel processing.
    verbose : str ['debug', 'triku', 'info', 'warning', 'error', 'critical']
        Logger verbosity output.
    Returns
    -------
    list_features : list
        list of selected features
    """
    # Todo make functions private if necessary

    # Basic checks of variables
    set_level_logger(verbose)

    for var in [n_features, knn, n_windows, n_procs, random_state, n_comps]:
        assert (var is None) | (isinstance(var, int)), "The variable value {} must be an integer!".format(var)

    if isinstance(object_triku, pd.DataFrame):
        use_adata_knn = False

    if n_procs is None:
        n_procs = max(1, get_cpu_count() - 1)
    elif n_procs > get_cpu_count():
        triku_logger.warning('The selected number of cpus ({}) is higher than the available number ({}). The number'
                             'of used cores will be set to {}.'.format(n_procs, get_cpu_count(),
                                                                       max(1, get_cpu_count() - 1)))
        n_procs = max(1, get_cpu_count() - 1)
    triku_logger.log(TRIKU_LEVEL, 'Number of processors set to {}'.format(n_procs))

    # Get the array of counts (np.array) and the array of genes.
    arr_counts, arr_genes = get_arr_counts_and_genes(object_triku, use_raw=use_raw)
    mean_counts, per_counts = return_mean(arr_counts), return_proportion_zeros(arr_counts)

    """
    First step is to get the kNN for the expression matrix.
    This is not that time intensive, but for reproducibility, we by default accept the kNN calculated by
    scanpy (sc.pp.neighbors()), and obtain the info from there. 
    Otherwise, we calculate the kNNs. 
    The expected output from this step is a matrix of cells x (kNN + 1), where each column includes the neighbor index
    of the cell number 
    """

    knn_array = None

    if isinstance(object_triku, sc.AnnData):
        if (use_adata_knn is None) or use_adata_knn:
            if 'neighbors' in object_triku.uns:
                knn = object_triku.uns['neighbors']['params']['n_neighbors']
                triku_logger.info('We found "neighbors" in the anndata, with knn={}. If you want to calculate the '
                                  'neighbors with triku, set use_adata_knn=False'.format(knn))

                # Connectivities array contains a pairwise relationship between cells. We want to select, for
                # each cell, the knn "nearest" cells. We can easily do that with argsort. In the end we obtain a
                # cells x knn array with the top knn cells.
                knn_array = np.asarray(object_triku.uns['neighbors']['connectivities'].todense()
                                       ).argsort()[:, -knn::][::, ::-1]

                # Last step is to add a arange of 0 to n_cells in the first column.
                knn_array = np.concatenate((np.arange(knn_array.shape[0]).reshape(knn_array.shape[0], 1),
                                            knn_array), axis=1)

    if knn_array is None:
        if knn is None:
            knn = int(0.5 * (arr_counts.shape[0]) ** 0.5)
            triku_logger.info('The number of neighbours is set to {}'.format(knn))

        triku_logger.info('Calculating knn indices')
        knn_array = return_knn_indices(arr_counts, knn=knn, return_random=False, random_state=random_state,
                                       metric=metric, n_comps=n_comps)

    # Calculate the expression in the kNN (+ own cell) for all genes
    triku_logger.info('Calculating knn expression')
    arr_knn_expression = return_knn_expression(arr_counts, knn_array)

    # Todo: for non integer values (alevin, kallisto, etc.) convolution must be discrete. We can divide the
    # unit in 10 or 20 subunits, transform the read count to the nearest "pseudointeger" value and apply the convolution
    # knn values will differ from the convolution, but with a "good" set of units it should be enough.

    # Apply the convolution, and calculate the EMD. The convolution is quite fast, but we will still paralellize it.
    triku_logger.info('Parallel emd calculation')
    triku_logger.log(TRIKU_LEVEL, 'min_knn set to {}'.format(min_knn))
    list_x_conv, list_y_conv, array_emd = parallel_emd_calculation(array_counts=arr_counts,
                                                                   array_knn_counts=arr_knn_expression,
                                                                   knn=knn, n_procs=n_procs, min_knn=min_knn)

    # Randomization!
    # Todo: study if it is possible to randomize with the same strategy as convolution for kallisto / alevin datasets.
    # The same steps must be applied to a randomized expression count matrix if we must
    list_x_conv_random, list_y_conv_random, array_emd_random = None, None, None
    arr_knn_expression_random, knn_array_random = None, None

    if apply_background_correction:
        triku_logger.info('Creating randomized count matrix')
        arr_counts_random = create_random_count_matrix(arr_counts)

        triku_logger.info('Calculating knn indices on randomized matrix')
        knn_array_random = return_knn_indices(arr_counts, knn=knn, return_random=False, random_state=random_state,
                                              metric=metric, n_comps=n_comps)

        triku_logger.info('Calculating knn expression on randomized matrix')
        arr_knn_expression_random = return_knn_expression(arr_counts_random, knn_array_random)

        triku_logger.info('Parallel emd calculation on randomized matrix')
        list_x_conv_random, list_y_conv_random, array_emd_random = \
            parallel_emd_calculation(array_counts=arr_counts_random, array_knn_counts=arr_knn_expression_random,
                                     knn=knn, n_procs=n_procs, min_knn=min_knn)

    # Apply emd distance correction (substract the emd to the random_emd)
    if array_emd_random is not None:
        array_emd_corrected = array_emd - array_emd_random
        array_emd_corrected[array_emd_corrected < 0] = 0
    else:
        array_emd_corrected = array_emd

    triku_logger.info('Subtracting median')
    array_emd_subt_median = subtract_median(x=mean_counts, y=array_emd_corrected, n_windows=n_windows)

    # Selection of best genes, either by the curve method or as the N highest ones.
    if n_features is None:
        triku_logger.info('Selecting cutoff point')
        dist_cutoff = get_cutoff_curve(y=array_emd_subt_median, s=s)
    else:
        dist_cutoff = np.sort(array_emd_subt_median)[- n_features]
    triku_logger.info('Cutoff point set to {}'.format(dist_cutoff))

    # Returns phase. Return if object is not an adata or if return is set to true.
    is_highly_variable = array_emd_subt_median > dist_cutoff
    if isinstance(object_triku, sc.AnnData):
        object_triku.var['highly_variable'] = is_highly_variable
        object_triku.var['emd_distance'] = array_emd_subt_median
        object_triku.var['emd_distance_uncorrected'] = array_emd
        object_triku.uns['triku_params'] = {'knn': knn}
        if array_emd_random is not None:
            object_triku.var['emd_distance_random'] = array_emd_random

    if do_return or (not isinstance(object_triku, sc.AnnData)):
        dict_return = {'highly_variable': is_highly_variable, 'emd_distance': array_emd_subt_median,
                       'emd_distance_uncorrected': array_emd}
        if array_emd_random is not None:
            dict_return['emd_distance_random'] = array_emd_random

        if triku_logger.level < logging.INFO:
            dict_return['knn_indices'], dict_return['knn_indices_random']  = knn_array, knn_array_random
            dict_return['knn_expression'], dict_return['knn_expression_random'] = arr_knn_expression, arr_knn_expression_random

            dict_return['x_convolution'], dict_return['x_convolution_random'] = list_x_conv, list_x_conv_random
            dict_return['y_convolution'], dict_return['y_convolution_random'] = list_y_conv, list_y_conv_random

            dict_return['array_counts'], dict_return['array_genes'] = arr_counts, arr_genes
        return dict_return
