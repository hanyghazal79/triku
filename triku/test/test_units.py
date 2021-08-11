import numpy as np
import pytest
import scanpy as sc
import scipy.sparse as spr

import triku as tk


@pytest.fixture
def getpbmc3k():
    adata = sc.datasets.pbmc3k()
    sc.pp.filter_genes(adata, min_cells=10)
    sc.pp.filter_cells(adata, min_genes=10)
    sc.pp.neighbors(adata)

    # tk.tl.triku(adata)

    return adata


@pytest.mark.exception_check
def test_triku_check_count_mat_20000_vars():
    adata = sc.datasets.blobs(
        n_variables=22000, n_centers=3, cluster_std=1, n_observations=500
    )
    adata.X = np.abs(adata.X).astype(int)
    print(adata.X)

    sc.pp.filter_cells(adata, min_genes=10)
    sc.pp.filter_genes(adata, min_cells=1)

    sc.pp.pca(adata)
    sc.pp.neighbors(adata)

    assert adata.X.shape[1] > 20000

    tk.tl.triku(adata)


@pytest.mark.exception_check
def test_triku_check_count_negative():
    adata = sc.datasets.blobs(
        n_variables=2000, n_centers=3, cluster_std=1, n_observations=500
    )
    assert np.min(adata.X) < 0

    try:
        tk.tl.triku(adata)
    except BaseException:
        pass
    else:
        raise BaseException


@pytest.mark.exception_check
def test_triku_check_null_genes():
    adata = sc.datasets.blobs(
        n_variables=2000, n_centers=3, cluster_std=1, n_observations=500
    )
    adata.X = np.abs(adata.X).astype(int)
    adata.X[:, 1] = 0

    try:
        tk.tl.triku(adata)
    except BaseException:
        pass
    else:
        raise BaseException


@pytest.mark.exception_check
def test_triku_check_nonunique_varnames():
    adata = sc.datasets.blobs(
        n_variables=2000, n_centers=3, cluster_std=1, n_observations=500
    )
    adata.X = np.abs(adata.X).astype(int)
    adata.var_names = [0] + list(np.arange(len(adata.var_names) - 1))

    try:
        tk.tl.triku(adata)
    except BaseException:
        pass
    else:
        raise BaseException


@pytest.mark.exception_check
def test_triku_check_nonaccepted_type():
    adata = sc.datasets.blobs(
        n_variables=2000, n_centers=3, cluster_std=1, n_observations=500
    )
    adata.X = np.abs(adata.X).astype(int)
    adata.var_names = [0] + list(np.arange(len(adata.var_names) - 1))

    try:
        tk.tl.triku(adata.X)
    except BaseException:
        pass
    else:
        raise BaseException


@pytest.mark.exception_check
def test_triku_dense_sparse_matrices(getpbmc3k):
    adata_dense = getpbmc3k
    adata_sparse = getpbmc3k.copy()

    adata_dense.X = adata_dense.X.toarray()
    adata_sparse.X = spr.csr.csr_matrix(adata_sparse.X)

    tk.tl.triku(adata_sparse)
    tk.tl.triku(adata_dense)

    assert adata_dense.uns["triku_params"] == adata_sparse.uns["triku_params"]
    assert np.all(
        adata_dense.var["triku_distance"] == adata_sparse.var["triku_distance"]
    )
