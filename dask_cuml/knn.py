# Copyright (c) 2018, NVIDIA CORPORATION.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
from .core import *

import dask

from cuml import KNN as cumlKNN
from cuml import device_of_ptr

import logging

import random

import itertools


from dask.distributed import get_worker, get_client, Client

from dask import delayed
from collections import defaultdict
from dask.distributed import wait, default_client
import dask.dataframe as dd
import dask.array as da


from tornado import gen
import dask_cudf, cudf

import logging

import os
import time


import numpy as np

from toolz import first, assoc

import numba.cuda


def to_gpu_matrix(df):

    try:
        gpu_matrix = df.as_gpu_matrix(order='F')
        dev = device_of_devicendarray(gpu_matrix)
        return dev, gpu_matrix

    except Exception as e:
        import traceback
        logging.error("Error in to_gpu_matrix(dev=" + str(dev) + "): " + str(e))
        traceback.print_exc()
        pass


def build_alloc_info(data):
    dev, gpu_matrix = data
    return gpu_matrix.__cuda_array_interface__


def get_ipc_handle(data):
    dev, gpu_matrix = data

    try:
        in_handle = gpu_matrix.get_ipc_handle()
        return dev, in_handle
    except Exception as e:
        import traceback
        logging.error("Error in get_ipc_handles(dev=" + str(dev) + "): " + str(e))
        traceback.print_exc()
        pass


# Run on a single worker on each unique host
def _fit_on_worker(data, params):
    ipcs, raw_arrs = data

    # Separate threads to hold pointers to separate devices
    # The order in which we pass the list of IPCs to the thread matters and the goal is
    # to maximize reuse while minimizing the number of threads. We want to limit the
    # number of threads to O(len(devices)) and want to avoid having if be O(len(ipcs))
    # at all costs!
    device_handle_map = defaultdict(list)
    [device_handle_map[dev].append(ipc) for dev, ipc in ipcs]

    open_ipcs = [new_ipc_thread(ipcs, dev) for dev, ipcs in device_handle_map.items()]

    alloc_info = list(itertools.chain(*[t.info() for t in open_ipcs]))
    alloc_info.extend([build_alloc_info(t) for t in raw_arrs])

    m = cumlKNN(should_downcast = params["should_downcast"])
    m._fit_mg(params["D"], alloc_info)

    [t.close() for t in open_ipcs]
    [t.join() for t in open_ipcs]

    return m


def _kneighbors_on_worker(data, m, params):

    print("DATA: " + str(data))
    print("params: "+ str(params))

    ipc_dev_list, devarrs_dev_list = data

    print(":::::::::::::::::::::::kneighbors_on_worker")

    print(ipc_dev_list)

    #TODO: One ipc thread per device instead of per x,y,coef tuple
    open_ipcs = []
    for p, dev, _ in ipc_dev_list:
        for x, i, d in p:
            ipct = new_ipc_thread([x, i, d], dev)
            open_ipcs.append(ipct)

    alloc_info = list(itertools.chain([t.info() for t in open_ipcs]))

    print("About to build alloc info for devicendarrays...")

    print("devarrs_dev_list=" + str(devarrs_dev_list))

    for p, dev, _ in devarrs_dev_list:
        for X, inds, dists in p:
            alloc_info.extend([[build_alloc_info((dev, X)),
                               build_alloc_info((dev, inds)),
                               build_alloc_info((dev, dists))]])

    print("alloc_info: " + str(alloc_info))

    for alloc in alloc_info:

        print("ALLOC: " + str(alloc))

        X, inds, dists = alloc
        m._query(X["data"][0], X["shape"][0], params["k"], inds["data"][0], dists["data"][0])

    [t.close() for t in open_ipcs]
    [t.join() for t in open_ipcs]

    return data


def input_to_device_arrays(X, params):
    """
    Create output arrays and return them w/ the input array(s)
    :param arr:
        A tuple in the form of (X, y)
    :return:
    """

    print("INDEX: " + str(X[0].index.values[0]))
    print("INDEX: " + str(X[0].index.values[-1]))

    start_idx = X[0].index.values[0]
    stop_idx = X[0].index.values[-1]

    X_mat = X[0].as_gpu_matrix(order="F")
    dev = device_of_devicendarray(X_mat)

    shape = X_mat.shape[0]*params["k"]

    # Create output numba arrays.
    I_ndarr = numba.cuda.to_device(np.zeros(shape, dtype=np.int64, order="F"))
    D_ndarr = numba.cuda.to_device(np.zeros(shape, dtype=np.float32, order="F"))

    # Return canonical device id as string
    return [(X_mat, I_ndarr, D_ndarr)], dev, (start_idx, stop_idx)


def get_input_ipc_handles(arr):
    arrs, dev, idx = arr
    ret = [(X.get_ipc_handle(), inds.get_ipc_handle(), dists.get_ipc_handle())
           for X, inds, dists in arrs]
    return ret, dev, idx


def build_dask_dfs(arrs, params):
    arr, dev, idx = arrs

    X, I_ndarr, D_ndarr = arr[0]

    I_ndarr = I_ndarr.reshape((X.shape[0], params["k"]))
    D_ndarr = D_ndarr.reshape((X.shape[0], params["k"]))

    I = cudf.DataFrame()
    for i in range(0, I_ndarr.shape[1]):
        I[str(i)] = I_ndarr[:, i]
    I = I.set_index(np.arange(idx[0], idx[1]+1))

    D = cudf.DataFrame()
    for i in range(0, D_ndarr.shape[1]):
        D[str(i)] = D_ndarr[:, i]
    D = D.set_index(np.arange(idx[0], idx[1]+1))

    I_ddf = dask_cudf.from_cudf(I, npartitions=1)
    D_ddf = dask_cudf.from_cudf(D, npartitions=1)

    return I_ddf, D_ddf


class KNN(object):
    """
    Data-parallel Multi-Node Multi-GPU kNN Model.

    Data is spread across Dask workers using Dask cuDF. On each unique host, a single worker is chosen to creates
    a series of kNN indices, one for each chunk of the Dask input, across devices on that host. Each unique hostname
    is assigned a monotonically increasing identifier, which is used as a multiplier for the resulting kNN indices
    across hosts so that the global index matrix, returned from queries, will reflect the global order.
    """

    def __init__(self, should_downcast = False):
        self.model = None
        self.master_host = None
        self.should_downcast = should_downcast

    def fit(self, ddf):
        """
        Fits a multi-node multi-gpu knn model, each node using their own index structure underneath.
        :param futures:
        :return:
        """
        client = default_client()

        # Keep the futures around so the GPU memory doesn't get
        # deallocated on the workers.
        gpu_futures, cols = client.sync(self._get_mg_info, ddf)

        host_dict = self._build_host_dict(gpu_futures, client).items()
        if len(host_dict) > 1:
            raise Exception("Dask cluster appears to span hosts. Current "
                            "multi-GPU implementation is limited to a single host")

        # Choose a random worker on each unique host to run cuml's kNN.fit() function
        # on all the cuDFs living on that host.
        self.master_host = [(host, random.sample(ports, 1)[0])
                            for host, ports in host_dict][0]

        host, port = self.master_host

        gpu_futures_for_host = list(filter(lambda d: d[0][0] == host, gpu_futures))
        exec_node = (host, port)

        # build ipc handles
        gpu_data_excl_worker = list(filter(lambda d: d[0] != exec_node, gpu_futures_for_host))
        gpu_data_incl_worker = list(filter(lambda d: d[0] == exec_node, gpu_futures_for_host))

        ipc_handles = [client.submit(get_ipc_handle, future, workers=[worker])
                       for worker, future in gpu_data_excl_worker]

        raw_arrays = [future for worker, future in gpu_data_incl_worker]

        f = (exec_node, client.submit(_fit_on_worker, (ipc_handles, raw_arrays),
                               {"D": cols, "should_downcast":self.should_downcast},
                               workers=[exec_node]))

        wait(f)

        # The model on each unique host is held for futures queries
        self.model = f

    @gen.coroutine
    def _kneighbors(self, X, k):

        client = default_client()

        # Break apart Dask.array/dataframe into chunks/parts
        data_parts = X.to_delayed()

        # Arrange parts into pairs.  This enforces co-locality
        parts = list(map(delayed, data_parts))
        parts = client.compute(parts)  # Start computation in the background
        yield wait(parts)

        for part in parts:
            if part.status == 'error':
                yield part  # trigger error locally

        # A dict in the form of { part_key: part }
        key_to_part_dict = dict([(str(part.key), part) for part in parts])

        who_has = yield client.who_has(parts)

        worker_parts = {}
        for key, workers in who_has.items():
            worker = parse_host_port(first(workers))
            if worker not in worker_parts:
                worker_parts[worker] = []
            worker_parts[worker].append(key_to_part_dict[key])

        """
        Create IP Handles on each worker hosting input data
        """
        # Format of input_devarrays = ([(X, y)..], dev)
        input_devarrays = [(worker, client.submit(input_to_device_arrays, part, {"k":k}, workers=[worker]))
                    for worker, part in worker_parts.items()]

        yield wait(input_devarrays)

        """
        Gather IPC handles for each worker and call _fit() on each worker containing data.
        """
        exec_node, model = self.model

        # Need to fetch coefficient parts on worker
        on_worker = list(filter(lambda x: x[0] == exec_node, input_devarrays))
        not_on_worker = list(filter(lambda x: x[0] != exec_node, input_devarrays))

        ipc_handles = [client.submit(get_input_ipc_handles, future, workers=[a_worker])
                       for a_worker, future in not_on_worker]

        raw_arrays = [future for a_worker, future in on_worker]

        # IPC Handles are loaded in separate threads on worker so they can be
        # used to make calls through cython

        run = client.submit(_kneighbors_on_worker, (ipc_handles, raw_arrays), model, {"k":k}, workers=[exec_node])
        yield wait(run)

        dfs = [client.submit(build_dask_dfs, f, {"k": k}, workers=[worker])
               for worker, f in input_devarrays]
        yield wait(dfs)

        return gen.Return(dfs)

    def kneighbors(self, X, k):

        """
        Queries the multi-gpu knn model given a dask-cudf as the query

        1. Create 2 new Dask dataframes to hold output (1 chunk each per chunk of X), co-locate pieces w/ X.
        2. Get IPC handles for each dataframe. Use IPCThread to hold onto them while calling query.

        :param input:
            A dask-cudf for calculating the kneighbors
        :param k:
            The number of nearest neighbors to query for each input vector.
        :return:
            dists and indices of the k-nearest neighbors to the input vectors
        """
        dfs = default_client().sync(self._kneighbors, X, k).value

        local_dfs = [f.result() for f in dfs]

        print("local_ddfs=" + str(local_dfs))

        print(str(default_client().who_has()))

        print("X_divisions: "+ str(X.divisions))

        I_ddf = dask_cudf.core.stack_partitions(
            [f[0] for f in local_dfs], X.divisions)
        D_ddf = dask_cudf.core.stack_partitions(
            [f[1] for f in local_dfs], X.divisions)

        print("DIVISIONS: " + str(I_ddf.divisions))
        return I_ddf, D_ddf


    def get(self, indices):
        """
        Returns the vectors from the knn index for a list of indices.
        :param indices:
        :return:
        """
        pass

    @staticmethod
    def _build_host_dict(gpu_futures, client):

        who_has = client.who_has(gpu_futures)

        key_to_host_dict = {}
        for key in who_has:
            key_to_host_dict[key] = parse_host_port(who_has[key][0])

        hosts_to_key_dict = {}
        for key, host in key_to_host_dict.items():
            if host not in hosts_to_key_dict:
                hosts_to_key_dict[host] = set([key])
            else:
                hosts_to_key_dict[host].add(key)

        workers = [key[0] for key in list(who_has.values())]
        return build_host_dict(workers)

    @gen.coroutine
    def _get_mg_info(self, ddf):

        client = default_client()

        if isinstance(ddf, dd.DataFrame):
            cols = len(ddf.columns)
            parts = ddf.to_delayed()
            parts = client.compute(parts)
            yield wait(parts)
        else:
            raise Exception("Input should be a Dask DataFrame")

        key_to_part_dict = dict([(str(part.key), part) for part in parts])
        who_has = yield client.who_has(parts)

        worker_map = []
        for key, workers in who_has.items():
            worker = parse_host_port(first(workers))
            worker_map.append((worker, key_to_part_dict[key]))

        gpu_data = [(worker, client.submit(to_gpu_matrix, part, workers=[worker]))
                    for worker, part in worker_map]

        yield wait(gpu_data)

        raise gen.Return((gpu_data, cols))
