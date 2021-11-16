from typing import List
from rhana.pattern import RheedConfig, RheedMask
from rhana.utils import _CM_rgb

from sklearn.cluster import DBSCAN
import plotly.graph_objects as go
import numpy as np

class DBSCANDistanceCluster:
    def __init__(self, eps=3, min_samples=1):
        self.model = DBSCAN(eps=eps, min_samples=min_samples)
    
    def _mean_dist(self, dists):
        all_cluster_mean = []

        for l in sorted(np.unique(self.labels)):
            # start from 0 to len(labels) or len(labels)-1 if it contains l="-1", 
            if l == -1 : all_cluster_mean.append(-1)
            cluster_mean = np.mean(dists[self.labels == l])
            all_cluster_mean.append(cluster_mean)
        
        self.mean_dists = np.array(all_cluster_mean)
        
        return self.mean_dists
    
    def _sort_dists(self, dists, mean_dists):
        sorted_mean_idx = np.argsort( mean_dists )
        self.mean_dists = mean_dists[sorted_mean_idx]
        
        mapper = np.zeros_like(sorted_mean_idx)
        for i, j in enumerate(sorted_mean_idx): mapper[j] = i
        
        self.labels = mapper[self.labels]
        
        return self.labels
        
    def fit_predict(self, dists):
        self.dists = dists
        self.model.fit_predict(dists)
        self.labels = self.model.labels_
        
        mean_dists = self._mean_dist(dists)
        labels = self._sort_dists(dists, mean_dists)
        
        return labels

class RHEEDMaskDistancePhaser:
    def __init__(self, rdms:List[RheedMask], convert_dist=False):
        # assume rdms already has distance computed
        self.rdms = rdms
        self.convert_dist = convert_dist
        
    def _get_all_distance(self):
        self.all_peak_dists = []

        for i, rdm in enumerate(self.rdms):
            for j, res in enumerate(rdm.collapses_peaks_flatten_ana_res):
                # the single item of all_peak_dists is a tuple like 
                # ((index_of_rdm in rdms, index of ana inana_res), average distance of that ana)
                peak_dist = res.avg_dist if not self.convert_dist else rdm.rd.config.hdist2G(res.avg_dist)
                self.all_peak_dists.append( ((i, j), peak_dist) )

        self.all_peak_dists_values = np.array(list(map(lambda x: x[1], self.all_peak_dists)))


    def run_cluster(self, eps=3, min_samples=1):
        self._get_all_distance()
        self.dc = DBSCANDistanceCluster(eps=eps, min_samples=min_samples)
        labels = self.dc.fit_predict(self.all_peak_dists_values[:, None])

        for rdm in self.rdms: rdm.cluster_labels = []

        for label, (idx, _) in zip( labels, self.all_peak_dists ):
            # idx[0] get the rdm id
            self.rdms[idx[0]].cluster_labels.append(label)

        for rdm in self.rdms:
            rdm.cluster_labels = np.array(rdm.cluster_labels)
            rdm.cluster_labels_unique = np.unique(rdm.cluster_labels)

    def get_intensity_map(self,):
        for rdm in self.rdms:
            rdm.get_group_intensity()


    def plot_intensity_map(self, x, y, name, xlabel, ylabel, log_x=False, log_y=False, x_space=5, y_space=5, max_num_row=5, text_x_space=0, text_y_space=3, reverse_x=False, reverse_y=False, scatter_size=15, cmap=_CM_rgb):
        """
            x: position on the xaxis
            y: position on the yaxis
            xlabel: x axis label
            ylabel: y axis label
        """
        def scale(x, space, factor, is_log):
            if is_log:
                x_scaled = x * (1+space * factor)
            else:
                x_scaled = x + space * factor
            return x_scaled

        def shifter(x, y, i, max_num):
            x_shift = scale(x, x_space, (i % max_num) - max_num // 2, log_x)
            y_shift = scale(y, y_space, i // max_num, log_y)
            return x_shift, y_shift

        def add_box(x, y, reverse_x, reverse_y):
            if reverse_x:
                x0 = scale(min(x), x_space, 1, log_x)
                x1 = scale(max(x), x_space, -1, log_x)            
            else:
                x0 = scale(min(x), x_space, -1, log_x)
                x1 = scale(max(x), x_space, 1, log_x)

            if reverse_y:
                y0 = scale(min(y), y_space, -0.5, log_y)
                y1 = scale(max(y), y_space, 1, log_y)
            else:
                y0 = scale(min(y), y_space, -0.6, log_y)
                y1 = scale(max(y), y_space, 0.75, log_y)

            fig.add_shape(type="rect",
                x0=x0, y0=y0, x1=x1, y1=y1,
                line=dict(
                    width=0,
                ),
                opacity=0.05,
                fillcolor="LightSkyBlue",
            )

            fig.add_shape(type="rect",
                x0=x0, y0=y0, x1=x1, y1=y1,
                line=dict(
                    color="Black",
                    width=0.5,
                ),
                opacity=1,
            )


        n_labels = len(np.unique(self.dc.labels))

        all_multihotencoding = np.zeros( (len(self.rdms), n_labels ) )
        all_percentage = np.zeros( (len(self.rdms), n_labels ) )

        for i, rdm, in enumerate(self.rdms):
            all_multihotencoding[i, rdm.cluster_labels_unique] = 1
            all_percentage[i, rdm.cluster_labels_unique] = rdm.group_percent

        fig = go.Figure()

        x, y = np.array(x), np.array(y)
        all_x, all_y = [], []
        for i in range(n_labels):
            x_shift, y_shift = shifter(x.copy(), y.copy(), i, max_num_row)

            all_x.append(x_shift)
            all_y.append(y_shift)
        all_x = np.stack(all_x)
        all_y = np.stack(all_y)

        for j in range(all_x.shape[1]):
            # add box first
            add_box(all_x[:, j], all_y[:, j], reverse_x, reverse_y)

        for i in range(n_labels):
            percent = all_percentage[:, i]
            dist = self.dc.mean_dists[i]

            # add the 
            fig.add_trace(
                go.Scatter(
                    x=all_x[i], y=all_y[i],
                    mode="markers",
                    name=f"{dist:.1f}",
                    marker=go.scatter.Marker(
                        color=cmap[i%len(cmap)],
                        symbol="square",
                        size=np.sqrt(percent)*scatter_size,
                        # line=go.scatter.marker.Line(color="black", width=1)
                    ),
                    text=[f"{p*100:.0f}%"for p in percent],
                    opacity=1,
                )
            )

        text_x = scale(x, x_space, text_x_space, log_x)
        text_y = scale(y, y_space, text_y_space, log_y)

        fig.add_trace(go.Scatter( x=text_x, y=text_y, mode="text", textposition="top center", text=name, showlegend=False, hovertext=None))

        if log_y:
            if reverse_y:
                fig.update_layout(yaxis=go.layout.YAxis(autorange="reversed", type="log", exponentformat="power"))
            else:
                fig.update_layout(yaxis=go.layout.YAxis( type="log", exponentformat="power"))
        if log_x:    
            if reverse_x:
                fig.update_layout(xaxis=go.layout.XAxis(autorange="reversed", type="log", exponentformat="power"))
            else:
                fig.update_layout(xaxis=go.layout.XAxis( type="log", exponentformat="power"))
        

        fig.update_xaxes(title=xlabel)
        fig.update_yaxes(title=ylabel)

        fig.update_layout(
            go.Layout(
                legend=go.layout.Legend(itemsizing='constant',),
                template="plotly_white",
                showlegend=True, 
            )
        )

        fig.update_shapes(dict(xref='x', yref='y'))

        return fig