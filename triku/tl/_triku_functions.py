import numpy as np
import scipy.stats as sts
import scipy.sparse as spr
from scipy.signal import fftconvolve

from sklearn.decomposition import PCA
from umap.umap_ import nearest_neighbors

import ray
from tqdm import tqdm
import logging
import gc

from triku.logg import triku_logger, TRIKU_LEVEL
from triku.genutils import TqdmToLogger


def get_n_divisions(arr_counts: np.array) -> int:
    if np.sum(arr_counts) == np.sum(arr_counts.view(int)):  # view is 2 to 10x faster than astype
        n_divisions = 1
    else:  # TODO: Make this more complex if we see that time is an important issue
        n_divisions = 10

    return n_divisions


def return_knn_indices(array: np.ndarray, knn: int, return_random: bool, random_state: int, metric: str,
                       n_comps: int) -> np.ndarray:
    """
    Given a expression array and a number of kNN, returns a n_cells x kNN matrix where each row, col is a
    neighbour of cell X.

    return_random attribute is used to assign random neighbours.
    """
    triku_logger.log(TRIKU_LEVEL, 'Calculating PCA for knn indices')
    pca = PCA(n_components=n_comps, whiten=True, svd_solver='auto', random_state=random_state).fit_transform(array)

    if return_random:
        triku_logger.log(TRIKU_LEVEL, 'Applying knn indices randomly')
        # With this approach it is possible that two knns are the same for a cell. But well, not really that important.
        knn_indices = np.random.randint(array.shape[0], array.shape[0] * knn).reshape(array.shape[0], knn)
        knn_indices[:, 0] = np.arange(array.shape[0])

    else:
        triku_logger.log(TRIKU_LEVEL, 'Calculating knn indices')
        knn_indices, knn_dists, forest = nearest_neighbors(pca, n_neighbors=knn, metric=metric,
                                                           random_state=np.random.RandomState(random_state),
                                                           angular=False, metric_kwds={})

    triku_logger.log(TRIKU_LEVEL, 'knn indices stats (shape | mean | std): {} | {} | {}'.format(knn_indices.shape,
                                                                                                np.mean(knn_indices),
                                                                                                np.std(knn_indices)))
    return knn_indices.astype(int)


def return_knn_expression(arr_expression: np.ndarray, knn_indices: np.ndarray) -> np.ndarray:
    """
    This function returns an array with the knn expression per gene and cell. To calculate the expression per gene
    we are going to apply the following procedure.

    First we create a 2D mask of neighbors. The mask is a translation of the knn_indices into a 2D sparse array,
    where the index i,j is 1 if cell j is neigbour of cell i and 0 elsewhere.

    That is, if we have n_g as number of genes, and n_c as number of cells, the matrix product would be:

        Mask          Expr         Result
    (n_c x n_c) · (n_c x n_g) = (n_c x n_g)

    Then, the Result matrix would have in each cell, the summed expression of that gene in the knn (and also the
    own cell).
    """

    sparse_mask = spr.lil_matrix((arr_expression.shape[0], arr_expression.shape[0]))
    # [:, 0] is [0,0,0,0,..., 0, 1, ..., 1, ... ] and [:, 1] are the indices of the rest of cells.
    sparse_mask[np.repeat(np.arange(knn_indices.shape[0]), knn_indices.shape[1]), knn_indices.flatten()] = 1
    triku_logger.log(TRIKU_LEVEL, 'sparse_mask sum {} / shape: {}'.format(sparse_mask.sum(), sparse_mask.shape))

    knn_expression = sparse_mask.dot(arr_expression)
    triku_logger.log(TRIKU_LEVEL, 'knn_expression: {} | {}'.format(knn_expression, knn_expression.shape))

    # Remember that we want the knn expression of the cells with positive expression! The rest are not interesting,
    # and must be discarded. So far
    knn_expression[arr_expression == 0] = 0
    return knn_expression


def create_random_count_matrix(matrix: np.array, random_state: int) -> np.ndarray:
    """
    Given a matrix with cells x genes, returns a randomized cells x genes matrix. This matrix has, for each genes,
    the counts of the gene from the original matrix dispersed across the cells. E.g., if gene X has 1000 across
    all cells counts, those counts are distributed randomly.
    """

    n_reads_per_gene = matrix.sum(0).astype(int)
    n_cells, n_genes = matrix.shape
    matrix_random = np.zeros((n_genes, n_cells))

    # The limiting part generally is the random number generation.
    # Random.choice is rather slow, so to save some time we use random.random, then multiply by the
    # number of cells, and change to int.
    np.random.seed(random_state)
    random_counts = np.random.randint(n_cells, size=np.sum(n_reads_per_gene))

    # Also, assigning values to a matrix is done by rows because it is 2 to 3 times faster than in rows.
    # Numpy rows are row-based so it will always be more efficient to do a row-wise assignment.
    idx_counts = 0
    for gene in range(n_genes):
        counts_gene = random_counts[idx_counts: idx_counts + n_reads_per_gene[gene]]
        bincount = np.bincount(counts_gene, minlength=n_cells)
        matrix_random[gene, :] = bincount
        idx_counts += n_reads_per_gene[gene]

    matrix_random = matrix_random.T
    return matrix_random


# TODO: apply for log-transformed data? The convolution works assuming that X data are integers.
def apply_convolution_read_counts(probs: np.ndarray, knn: int, func: [np.convolve, fftconvolve]) -> (np.ndarray, np.ndarray):
    """
    Convolution of functions. The function applies a convolution using np.convolve
    of a probability distribution knn times. The result is an array of N elements (N arises as the convolution
    of a n-length array knn times) where the element i has the probability of i being observed.

    Parameters
    ----------
    probs : np.array
        Object with count matrix. If `pandas.DataFrame`, rows are cells and columns are genes.
    knn : int
        Number of kNN
    """
    # We are calculating the convolution of cells with positive expression. Thus, in the first distribution
    # we have to remove the cells with 0 reads, and rescale the probabilities.
    arr_0 = probs.copy()
    arr_0[0] = 0  # TODO: this will fail in log-transformed data
    arr_0 /= arr_0.sum()

    # We will use arr_bvase as the array with the read distribution
    arr_base = probs.copy()

    arr_convolve = func(arr_0, arr_base, )

    for knni in range(2, knn):
        arr_convolve = func(arr_convolve, arr_base, )

    # TODO: check the probability sum is 1 and, if so, remove
    arr_prob = arr_convolve / arr_convolve.sum()

    # TODO: if log transformed, this is untrue. Should not be arange.
    return np.arange(len(arr_prob)), arr_prob


def nonnegative_fft(arr_a, arr_b):
    conv = fftconvolve(arr_a, arr_b)
    conv[conv < 0] = 0
    return conv


def compute_conv_idx(counts_gene: np.ndarray, knn: int) -> (np.ndarray, np.ndarray, np.ndarray):
    """
    Given a GENE x CELL matrix, and an index to select from, calculates the convolution of reads for that gene index.
    The function returns the
    """
    y_probs = np.bincount(counts_gene.astype(int)) / len(counts_gene) # Important to transform count to probabilities
    # to keep the convolution constant.

    if np.sum(counts_gene) > 7000:  # For low counts (< 5000 to < 10000), fttconvolve is 2-3 to 10 times faster.
        x_conv, y_conv = apply_convolution_read_counts(y_probs, knn=knn, func=nonnegative_fft)
    else:
        x_conv, y_conv = apply_convolution_read_counts(y_probs, knn=knn, func=np.convolve)

    return x_conv, y_conv, y_probs


def calculate_emd(knn_counts: np.ndarray, x_conv: np.ndarray, y_conv: np.ndarray, n_divisions: int) -> \
        (np.ndarray, np.ndarray):
    """
    Returns "normalized" earth movers distance (EMD). The function calculates the x positions and probabilities
    of the "real" dataset using the knn_counts, and the x positions and probabilities of the convolution as attributes.

    To normalize the distance, it is divided by the standard deviation of the convolution. Since the convolution
    is already given as a distribution, mean and variance have to be calculated "by hand".
    """
    dist_range = np.arange(max(knn_counts) + 1)
    # np.bincount transforms [3, 3, 4, 1, 2, 9] into [0, 1, 1, 2, 1, 0, 0, 0, 0, 1]
    real_vals = np.bincount(knn_counts.astype(int)) / len(knn_counts)

    # IMPORTANT: either for std or emd calculation, all x variables must be scaled back!
    real_vals /= n_divisions
    x_conv /= n_divisions

    emd = sts.wasserstein_distance(dist_range, x_conv, real_vals, y_conv)

    mean = (x_conv * y_conv).sum()
    std = np.sqrt(np.sum(y_conv * (x_conv - mean) ** 2))

    return x_conv, emd / std


def compute_convolution_and_emd(array_counts: np.ndarray, array_knn_counts: np.ndarray, idx: int,
                                knn: int, min_knn: int, n_divisions: int) -> (np.ndarray, np.ndarray, np.ndarray):
    counts_gene = array_counts[idx, :].ravel()  # idx is chosen by rows, because it is more effective!
    knn_counts = array_knn_counts[idx, :].ravel()
    knn_counts = knn_counts[knn_counts > 0]  # Remember that only knn expression from positively-expressed cells
    # From the previous step at the knn calculation we set knn expression from non-expressing cells to 0

    counts_gene = (counts_gene * n_divisions).astype(int)
    knn_counts = (knn_counts * n_divisions).astype(int)

    if np.sum(counts_gene > 0) > min_knn:
        # triku_logger.log(TRIKU_LEVEL, 'Convolution on index {}: counts = {}, per_zero = {}'.format(idx,
        #                 np.sum(counts_gene), np.sum(counts_gene == 0)/len(counts_gene)))

        x_conv, y_conv, y_probs = compute_conv_idx(counts_gene, knn)
        x_conv, emd = calculate_emd(knn_counts, x_conv, y_conv, n_divisions)
    else:
        y_conv = np.bincount(knn_counts.astype(int))
        x_conv = np.arange(len(y_conv))
        emd = 0

    return x_conv, y_conv, emd


def parallel_emd_calculation(array_counts: np.ndarray, array_knn_counts: np.ndarray,
                             n_procs: int, knn: int, min_knn: int, n_divisions: int) -> (list, list, np.ndarray):
    """
    Calculation of convolution for each gene, and its emd. To do that we call compute_convolution_and_emd which,
    in turn, calls compute_conv_idx to calculate the convolution of the reads; and calculate_emd, to calculate the
    emd between the convolution and the knn_counts.

    Since we are working with counts, rather than an adata, we transpose the arrays so that the expression of each gene
    is a row. This makes a difference in time consumption (after 20000 genes, of course).

    To make things faster we use ray parallelization. Ray selects the counts and knn counts on each gene, and computes
    the convolution and distance. The output result is, for each gene, the convolution distribution
    (x, and probabilities), and the distances.
    """
    n_genes = array_counts.shape[1]

    # Apply a non_paralellized variant with tqdm
    if n_procs == 1:
        tqdm_out = TqdmToLogger(triku_logger, level=logging.INFO)

        return_objs = [compute_convolution_and_emd(array_counts.T, array_knn_counts.T, idx_gene, knn, min_knn,
                                                   n_divisions)
                       for idx_gene in tqdm(range(n_genes), file=tqdm_out)]

    else:
        ray.shutdown()
        ray.init(num_cpus=n_procs, ignore_reinit_error=True)

        compute_convolution_and_emd_remote = ray.remote(compute_convolution_and_emd)
        array_counts_id = ray.put(array_counts.T)  # IMPORTANT TO TRANSPOSE TO SELECT ROWS (much faster)!!!
        array_knn_counts_id = ray.put(array_knn_counts.T)

        ray_obj_ids = [compute_convolution_and_emd_remote.remote(array_counts_id, array_knn_counts_id, idx_gene,
                                                                 knn, min_knn, n_divisions)
                       for idx_gene in range(n_genes)]

        triku_logger.log(TRIKU_LEVEL, 'Parallel computation of distances.')
        return_objs = ray.get(ray_obj_ids)
        triku_logger.log(TRIKU_LEVEL, 'Done.')

        del [array_counts_id, array_knn_counts_id]; gc.collect()
        ray.shutdown()

    list_x_conv, list_y_conv, list_emd = [x[0] for x in return_objs], [x[1] for x in return_objs], [x[2] for x in
                                                                                                    return_objs]
    # list_x_conv and list_y_conv are lists of lists. Each element are the x coordinates and probabilities of the
    # convolution distribution for a gene. list_emd is an array with n_genes elements, where each element is the
    # distance between the convolution and the knn_distribution
    return list_x_conv, list_y_conv, np.array(list_emd)


def subtract_median(x, y, n_windows):
    """We working with EMD, we want to find genes with more deviation on emd compared with other genes with similar
    mean expression. With higher expressions EMD tends to increase. To reduce that basal level we will substract the
    median EMD to the genes using a number of windows. The approach is quite reliable between 15 and 80 windows.

    Too many windows can over-normalize, and lose genes that have high emd but are alone in that window."""

    # We have to take the distance in logarithm to account for the wide expression ranges
    linspace = 10**np.linspace(np.min(np.log10(x)), np.max(np.log10(x)), n_windows + 1)
    y_adjust = y.copy()

    y_median_array = np.zeros(len(y))
    for i in range(n_windows):
        mask = (x >= linspace[i]) & (x <= linspace[i + 1])
        y_median_array[mask] = np.median(y[mask])

    y_adjust -= y_median_array

    return y_adjust


def get_cutoff_curve(y, s):
    """
    Plots a curve, and finds the best point by joining the extremes of the curve with a line, and selecting
    the point from the curve with the greatest distance.
    The distance between a point in a curve, and the straight line is set by the following equation
    if u,v is the point in the curve, and y = mx + b is the line, then
    x_opt = (u - mb + mv) / (1 + m^2)

    Here y attribute refers to the emd distances (after median subtraction preferably). Those distances are sorted,
    and ordered, and the curve is extracted from there.
    """

    min_y, max_y = np.min(y), np.max(y)
    m, b = (max_y - min_y) / len(y), min_y

    list_d = []

    for u, v in enumerate(np.sort(y)):
        x_opt = (u - m * b + m * v) / (1 + m ** 2)
        y_opt = x_opt * m + b
        d = (x_opt - u) ** 2 + (y_opt - v) ** 2

        list_d.append(d)

    # S is a corrector factor. It leverages the best value in the curve, and selects a more or less stringent
    # value in the curve. the maximum distance is multiplied by (1 - S), and the leftmost or rightmost index
    # is selected

    dist_s = (1 - np.abs(s)) * np.max(list_d)
    s_idx = np.argwhere(list_d >= dist_s)

    if s >= 0:
        max_d_idx = np.max(s_idx)
    else:
        max_d_idx = np.min(s_idx)

    return np.sort(y)[max_d_idx]
