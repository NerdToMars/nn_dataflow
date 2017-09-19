""" $lic$
Copyright (C) 2016-2017 by The Board of Trustees of Stanford University

This program is free software: you can redistribute it and/or modify it under
the terms of the Modified BSD-3 License as published by the Open Source
Initiative.

If you use this program in your research, we request that you reference the
TETRIS paper ("TETRIS: Scalable and Efficient Neural Network Acceleration with
3D Memory", in ASPLOS'17. April, 2017), and that you send us a citation of your
work.

This program is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the BSD-3 License for more details.

You should have received a copy of the Modified BSD-3 License along with this
program. If not, see <https://opensource.org/licenses/BSD-3-Clause>.
"""

import itertools

from .layer import ConvLayer
from .network import Network
from .pipeline_segment import PipelineSegment
from .resource import Resource

class InterLayerPipeline(object):
    '''
    Inter-layer pipeline.
    '''

    def __init__(self, network, batch_size, resource, max_util_drop=0.05):
        if not isinstance(network, Network):
            raise TypeError('InterLayerPipeline: network must be '
                            'a Network instance.')
        if not isinstance(resource, Resource):
            raise TypeError('InterLayerPipeline: resource must be '
                            'a Resource instance.')
        if not 0 <= max_util_drop <= 1:
            raise ValueError('InterLayerPipeline: max_util_drop must be '
                             'between [0, 1].')

        self.network = network
        self.batch_size = batch_size
        self.resource = resource
        self.max_util_drop = max_util_drop

        self._calc_sched_dag()

        # Vertices starting from which we have generated the segments.
        self.seg_vertex_done = set()

    def ordered_layer_list(self):
        '''
        Get a list of the layers in their topological order in the scheduling
        DAG.
        '''
        return list(sum(self.dag_vertex_list, tuple()))

    def gen_segment(self, options):
        '''
        Generate all valid inter-layer pipelining segments.
        '''

        kwargs = {'network': self.network,
                  'batch_size': self.batch_size,
                  'resource': self.resource,
                  'max_util_drop': self.max_util_drop,
                 }

        # No pipelining, each vertex sequentially occupies the whole resource.
        for layer in self.network:
            seg = ((layer,),)
            segment = PipelineSegment(seg, **kwargs)
            assert segment.valid
            yield segment

        # Pipelining.
        for vseg in self._gen_vseg():

            # Use set to eliminate duplicates.
            seg_cands = set()

            if options.partition_interlayer:
                # Spatial pipelining.
                seg = tuple(self.dag_vertex_list[vidx] for vidx in vseg)
                seg_cands.add(seg)

            if options.hw_gbuf_save_writeback:
                # Temporal pipelining.
                # Reduce the spatial dimension.
                seg = (tuple(itertools.chain.from_iterable(
                    self.dag_vertex_list[vidx] for vidx in vseg)),)
                seg_cands.add(seg)

            # Determine segment allocation.
            for seg in seg_cands:
                segment = PipelineSegment(seg, **kwargs)
                if segment.valid:
                    yield segment

    def _gen_vseg(self, vertex_idx=0, done=None):
        '''
        Generate vertex segments starting from vertex `vertex_idx`. Yield a
        tuple of the vertices in the segment.

        `done` is a set of vertices which have already been scheduled and the
        output is already in memory.

        Rules:

        1. If a vertex does not share any dependencies with the current
        segment, i.e., none of its previous vertices is in the current segment
        or among the previous vertices of the current segment, we do not add it
        to the segment, because there is no benefit to co-locate them.

        2. If a vertex has multiple previous vertices, none of them
        can be in the same segment as this vertex, because the output data
        availability timing of the previous vertices may not match.

        3. If a vertex has multiple next vertices, all or none of them can be
        in the same segment as this vertex, because only including a subset of
        the next vertices cannot eliminate the data write-back to memory.
        '''

        vseg = tuple()

        if not done:
            done = set()
            # Reset.
            self.seg_vertex_done = set()

        if self.dag_input_vertex not in done:
            # Input layer is always in memory.
            done.add(self.dag_input_vertex)

        # The frontier is the vertex to be considered to be added to the
        # current segment.
        for frontier in range(vertex_idx, len(self.dag_vertex_list)):

            # Check whether the frontier can be added to the current segment.

            frontier_prevs = self.dag_prev_dict[frontier]

            # Whether the frontier share dependencies with the current segment,
            # if the segment is not empty.
            share_deps = not vseg or not frontier_prevs.isdisjoint(
                set.union(set(vseg), *[self.dag_prev_dict[i] for i in vseg]))

            # Whether some of the multiple previous vertices are in the current
            # segment.
            coupled_prevs = len(frontier_prevs) > 1 \
                    and not frontier_prevs.isdisjoint(vseg)

            if not share_deps or coupled_prevs:
                # Not sharing any dependencies (rule 1), or previous vertices
                # overlap with the current segment (rule 2).

                # Make sure the current segment is not empty.
                assert vseg
                # Not extend the segment any more. Note that the current
                # segment has already been yielded, as well as the recursion,
                # in the last iteration.
                break

            # Extend the segment.
            vseg += (frontier,)

            # Check whether the segment is valid.

            for idx in vseg:
                nexts = self.dag_next_dict[idx]

                # The next vertices should either all or none in the segment
                # (rule 3).
                if not (nexts.isdisjoint(vseg) or nexts.issubset(vseg)):
                    # The segment is invalid. Need to add more vertices.
                    assert min(nexts.difference(vseg)) > frontier
                    break
            else:
                # The segment is valid.
                yield vseg

                # Skip if have done.
                if frontier + 1 in self.seg_vertex_done:
                    continue

                # Recursion.
                for tpl in self._gen_vseg(frontier + 1, done.union(vseg)):
                    yield tpl

        assert vertex_idx not in self.seg_vertex_done
        self.seg_vertex_done.add(vertex_idx)

    def _calc_sched_dag(self):
        '''
        Build the scheduling DAG of the network. We merge layers with no
        filters into their last previous layer, so a DAG vertex can contain one
        or more layers.

        We order and index the DAG vertices in their depth-first topological
        order. This will also be the order to schedule the layers.

        Also establish two dicts for the previous and next vertices of each DAG
        vertex.

        Also record the number of operations of each DAG vertex.

        In summary, the attributes initialized include: `dag_input_vertex`,
        `dag_vertex_list`, `dag_vertex_dict`, `dag_prev_dict`, `dag_next_dict`.
        '''

        # Vertex of the input layer.
        self.dag_input_vertex = -1

        # The DAG vertex set. Each vertex is a merged layer tuples, represented
        # by their layer names. Use a list type to make modification easier.
        dag_vertex_set = []

        for layer_name in self.network:
            layer = self.network[layer_name]

            if isinstance(layer, ConvLayer):
                dag_vertex_set.append((layer_name,))

            else:
                prev_layers, _ = self.network.prev_layers(layer_name)
                prev_layers = set(prev_layers)
                assert prev_layers

                # Find and merge to a vertex if that vertex only contains one
                # previous layer at the last, because non-last previous layer
                # will not have its data available to be used for this layer.
                # Also the previous layer can only have this one next layer,
                # because its data will be overwritten by this layer locally.

                # Check vertices in the reversed order.
                for idx in reversed(range(len(dag_vertex_set))):
                    vhead = dag_vertex_set[idx][:-1]
                    vtail = dag_vertex_set[idx][-1]
                    if prev_layers.isdisjoint(vhead) and vtail in prev_layers \
                            and len(self.network.next_layers(vtail)) == 1:
                        dag_vertex_set[idx] += (layer_name,)
                        break
                else:
                    # No valid vertex to merge.
                    dag_vertex_set.append((layer_name,))

        assert sum(len(v) for v in dag_vertex_set) == len(self.network)

        # The DAG vertex list in the topological order.
        self.dag_vertex_list = self._topological_order(dag_vertex_set)

        # Make a directory from layer name to DAG vertex index.
        self.dag_vertex_dict = {}

        for vidx, v in enumerate(self.dag_vertex_list):
            for layer_name in v:
                assert layer_name not in self.dag_vertex_dict
                self.dag_vertex_dict[layer_name] = vidx

        # Add the input layer.
        self.dag_vertex_dict[self.dag_input_vertex] = \
                self.network.INPUT_LAYER_KEY

        # The previous and next relationship of the DAG vertices.
        self.dag_prev_dict = dict((vidx, set()) for vidx
                                  in range(len(self.dag_vertex_list)))
        self.dag_next_dict = dict((vidx, set()) for vidx
                                  in range(len(self.dag_vertex_list)))

        for layer_name in self.network:
            vidx = self.dag_vertex_dict[layer_name]

            # Previous layers.
            prev_layers, _ = self.network.prev_layers(layer_name)
            for pl in prev_layers:
                pvidx = self.dag_vertex_dict[pl] if pl \
                        else self.dag_input_vertex
                if pvidx != vidx:
                    self.dag_prev_dict[vidx].add(pvidx)

            # Next layers.
            next_layers = self.network.next_layers(layer_name)
            for nl in next_layers:
                if not nl:
                    continue
                nvidx = self.dag_vertex_dict[nl]
                if nvidx != vidx:
                    self.dag_next_dict[vidx].add(nvidx)

        # Add next layers of the input layer.
        self.dag_next_dict[self.dag_input_vertex] = set()
        for vidx in self.dag_prev_dict:
            if self.dag_input_vertex in self.dag_prev_dict[vidx]:
                self.dag_next_dict[self.dag_input_vertex].add(vidx)

    def _topological_order(self, dag_vertex_set):
        '''
        Order the DAG vertices in topological order using DFS.

        Specifically, The backtrace order of the depth-first search is the
        inverse of the topological order. See
        https://en.wikipedia.org/wiki/Topological_sorting#Depth-first_search
        '''

        # The visited layers in the DFS order.
        visited = []
        # The unseen pending layers.
        unseen = set(dag_vertex_set)
        # The layers that have been seen, but not visited due to unvisited
        # previous layers.
        seen = set()

        def _dfs(vertex):
            assert vertex not in seen and vertex not in visited

            unseen.discard(vertex)
            seen.add(vertex)

            next_layers = []
            for l in vertex:
                for nl in self.network.next_layers(l):
                    if nl and nl not in vertex and nl not in next_layers:
                        next_layers.append(nl)

            # Visit next layers in the reversed order, so the reversed visit
            # order has the original order.
            next_vertices = []
            for nl in reversed(next_layers):
                for nv in unseen:
                    if nl in nv:
                        next_vertices.append(nv)

            for nv in next_vertices:
                _dfs(nv)

            visited.append(vertex)
            seen.remove(vertex)

        # Start from the first layers.
        start_vertices = []
        for l in reversed(self.network.first_layers()):
            for v in unseen:
                if l in v:
                    start_vertices.append(v)
        for v in start_vertices:
            _dfs(v)
        assert not unseen
        assert not seen

        return list(reversed(visited))

