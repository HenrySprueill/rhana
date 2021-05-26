import numpy as np
from scipy.ndimage.filters import gaussian_filter1d
from scipy.optimize import curve_fit
from typing import List, Dict, Union

from dataclasses import dataclass, field
from collections import namedtuple

from scipy.signal import find_peaks
from scipy.spatial import distance_matrix

from scipy.interpolate import interp1d

from lmfit import models as lm_models

from rhana.utils import _create_figure, crop

def gaussian(x, A, x0, sig):
    return A*np.exp(-(x-x0)**2/(2*sig**2))


def multi_gaussian(x, *pars):
    offset = pars[-1]
    summation = offset
    for i in range(len(pars)//3):
        g = gaussian(x, pars[i*3], pars[i*3+1], pars[i*3+2])
        summation += g
    return summation


def get_peaks_distance(peaks, peaks_w, full=False, polar=True):
    """
        Given peaks of a list of spectrum, this function calculate the
        inter-peak distance for all peaks in one spectrum
        
        Argument:
            peaks : peaks of a list of spectrum
            ws: 
    """
    if len(peaks_w.shape) == 1:
        peaks_w = peaks_w[:, None]
    if not full:
        dm = distance_matrix(peaks_w, peaks_w, p=1)
        interdist = dm[np.tril_indices_from(dm, -1)]
    else:
        interdist = distance_matrix(peaks_w, peaks_w)

        # make the distance matrix polarize
        if polar: interdist[np.tril_indices_from(interdist)] = -interdist[np.tril_indices_from(interdist)]

    return interdist


def create_grid(start, end, center, dist):
    """ a grid with spaceing 'dist', that center at 'center', range from 'start - center' to 'end - center' """
    rn = int((end - center) // dist)
    ln = int((start - center) // dist)
    return np.arange(ln+1, rn+1) * dist


def get_center_peak_idxs(peaks, spec_center_loc, tolerant):
    return [i for i, p in enumerate(peaks) if abs(p - spec_center_loc) < tolerant]


def get_center_peak_idx(peaks, spec_center_loc, tolerant):
    ci = np.argmin( abs(peaks - spec_center_loc) )
    if peaks[ci] - spec_center_loc < tolerant : return ci
    else: return -1


def get_all_nbr_idxs(center_i, idxs):
    """
        examples

        center_i = 3
        idx = 0, 1, 2, 3, 4, 

        what it yields in order:
            2, 4, 1, 0
    """
    level = 0
    endleft = False
    endright = False
    maxidx = max(idxs)

    while not (endleft and endright):
        level = level + 1
        if center_i - level >= 0:
            yield center_i - level
        else:
            endleft = True
        if center_i + level <=maxidx:
            yield center_i + level
        else:
            endright = True


def analyze_peaks_distance_cent(peaks, center_nbr_dists, center_peak, center_peak_i, grid_min_w, grid_max_w, tolerant=0.01, abs_tolerant=10, allow_discontinue=1): 
    mask = np.zeros((len(peaks), len(peaks)), dtype=bool)
    out = []

    cp = center_peak
    ci = center_peak_i

    for j in get_all_nbr_idxs(ci, np.arange(len(peaks))):
        if ci == j : continue
        if mask[ci, j] : continue

        dist = abs(center_nbr_dists[j])

        if dist <= abs_tolerant: continue # avoid picking up point near the center points
        grid = create_grid(grid_min_w, grid_max_w, cp, dist)

        nbr_grid_dm = abs(distance_matrix(center_nbr_dists[:, None], grid[:, None])) # this step could be optimize to O(n)
        close = nbr_grid_dm.argmin(axis=1) # a array of peak id that has the shortest distance to one of the tick in the grid 
        
        match_error = nbr_grid_dm[np.arange(len(center_nbr_dists)), close] # get the distance of those peaks to the grid

        select = np.abs(match_error) < min(tolerant * dist, abs_tolerant) # (binary) pick those with acceptable distance

        idx = np.where(select)[0] # (center neighbor integar index) 

        gidx = close[ select ] # (grid integar index)
        uni_gidx = np.unique(gidx)
        uni_gidx.sort()

        continuity = np.diff(uni_gidx) # (ses if there are disconnection)
        allowed = np.all(continuity <= allow_discontinue+1) # (selected if only the pattern has no/less disconnection)

        if allowed and len(uni_gidx) > 1:
            x, y = np.meshgrid(idx, idx)
            mask[x, y] = True # label those selected pick-pick distance as processed, won't touch if again

            selected_multi = grid[gidx] / dist
            
            nonzero_multi = selected_multi != 0

            avg_dist = np.sum( center_nbr_dists[idx][nonzero_multi] / selected_multi[nonzero_multi] ) / (sum(nonzero_multi))
            avg_err = (np.sum( abs(center_nbr_dists[idx][nonzero_multi] / selected_multi[nonzero_multi] - avg_dist) ) ) / (sum(nonzero_multi))

            out.append( PeakAnalysisResult(peaks_family=idx, avg_dist=avg_dist, avg_err=avg_err, detail=None) )
        
    return out


@dataclass
class PeakAnalysisDetail:
    tpd: float
    matched: list


@dataclass
class PeakAnalysisResult:
    peaks_family: np.ndarray
    avg_dist: float
    avg_err: float
    detail: PeakAnalysisDetail


@dataclass
class Spectrum:
    spec: np.ndarray # spectrum intensity
    ws: np.ndarray

    def _update_spectrum(self, spec, ws, inplace=True):
        if inplace:
            self.spec = spec
            self.ws = ws
            return self
        else:
            return Spectrum(spec, ws)

    def normalization(self, inplace=True):
        """
            normalize the spectrum by min, max value
            if min max is not given then it would be computed from the given spectrum
        """
        
        _min = self.spec.min()
        _max = self.spec.max()
        
        nspec = (self.spec - _min) / (_max - _min + 1e-5)

        return self._update_spectrum(nspec, self.ws, inplace=inplace)

    def smooth(self, sigma=1, inplace=True, **kargs):
        nspec = gaussian_filter1d(self.spec, sigma=sigma, **kargs)
        return self._update_spectrum(nspec, self.ws)

    def clip(self, clip_min, clip_max, inplace=True):
        return self._update_spectrum( np.clip( self.spec, clip_min, clip_max ), self.ws, inplace=inplace )

    def remove_background(self, n=2, inplace=True):
        """ 
            Assuming the background error is in linear form.
            Fit a linear line from n data points at the beginning and the end of the spectrum.
            Subtract the spacetrum by the fitted linear intensity.

            Argument :
                n : number of entries from front and tail to be consider
        """
        if n > 1:
            X = np.concatenate( (self.ws[0:n], self.ws[-n:]) )
            Y = np.concatenate( (self.spec[0:n], self.spec[-n:]) )
        else:
            X = np.array([self.ws[0], self.ws[-1]])
            Y = np.array([self.spec[0], self.spec[-1]])
        X = np.stack( (X, np.ones_like(X)), axis=1 )
        Y = Y.T

        A = np.linalg.inv(X.T@X)@(X.T@Y)
        
        pX = np.stack( (self.ws, np.ones_like(self.ws)), axis=1 )
        nspec = self.spec - (pX @ A)
        return self._update_spectrum( nspec, self.ws, inplace=inplace)


    def filling_flat(self, trunc=0.99, inplace=True):
        """
            Filling truncated area with quadratic spline. Return a
            new PseudoLaueCircleSpectrum if there are area to fill
            else return the original object
            
            Arguments:
                trunc : maximum value where signal is truncated
        """
        
        # we fill it by the quadratic curve
        ws, spec = self.ws, self.spec
        smask = spec <= trunc
        if smask.sum() < len(spec) and smask.sum() > 2:
            f = interp1d(ws[smask], spec[smask], kind="quadratic")
            spec = f(ws)

        if inplace:
            self.spec = spec
            return self
        else:
            return Spectrum(spec, self.ws)

    def fit_spectrum_peaks(self, height=0.001, threshold=0.001, prominence=0.10, **peak_args):
        """
            Find Peaks over the list of spectrum. 
            
            Arguments:
                thres : background noise level, see find_peaks ref to more detail
                prominence : peak prominence, see find_peaks ref to more detail 
        """
        peaks, peaks_info = find_peaks(self.spec, height=height, threshold=threshold, prominence=prominence, **peak_args)

        self.peaks = peaks
        self.peaks_info = peaks_info

        return peaks, peaks_info

    def get_peaks_distance(self, full=False, polar=True):
        """
            Given peaks of a list of spectrum, this function calculate the
            inter-peak distance for all peaks in one spectrum
            
            Argument:
                full : get the full inter peak distance matrix or only get the upper triangle
                polar : make d[i, j] = -d[j, i]
        """
        interdist = get_peaks_distance(self.peaks, self.ws[self.peaks], full, polar)

        self.interdist = interdist
        
        return interdist

    def analyze_peaks_distance_cent(self, tolerant=0.01, abs_tolerant=10, allow_discontinue=1):
        grid_max_w = max(self.ws)
        grid_min_w = 0

        interdist = self.get_peaks_distance(full=True, polar=True) # a peak-peak distance matrix
        interdist[np.tril_indices_from(interdist)] = -interdist[np.tril_indices_from(interdist)] # make the distance matrix polarize

        center_peak_i = get_center_peak_idx(self.peaks, int(len(self.ws)//2) , abs_tolerant)
        if center_peak_i == -1 : return []
        center_peak = self.peaks[center_peak_i]
        
        center_nbr_dists = interdist[center_peak_i, :]
        
        return analyze_peaks_distance_cent(self.peaks, center_nbr_dists, center_peak, center_peak_i, grid_min_w, grid_max_w, tolerant, abs_tolerant, allow_discontinue)

    def plot_spectrum(self, ax=None, peaks=None, peakgroups=None, offset=0, peak_offset=0, showlegend=False, exclusive=True, **fig_kargs):
        # peaks, peaksinfo = peaks
        fig, ax = _create_figure(ax=ax, **fig_kargs)
        peak_offset_arr = np.zeros_like(peaks, dtype=float)

        ax.plot(self.ws, self.spec+offset)

        if peaks is not None:
            if peakgroups is not None:
                for g, dist in peakgroups[:-1]:
                    ax.plot(self.ws[peaks[g]], self.spec[peaks[g]]+offset+peak_offset_arr[g], "x", label=f"dist={dist:.1f}")
                    peak_offset_arr[g] += peak_offset

                g, _ = peakgroups[-1]
                ax.plot(self.ws[peaks[g]], self.spec[peaks[g]]+offset+offset+peak_offset_arr[g], "o")
            else:
                ax.plot(self.ws[peaks], self.spec[peaks]+offset, "x")
        
        if showlegend: ax.legend()
        return fig, ax

    @staticmethod        
    def get_peaks_group(ana_res, peaks, exclusive=True):
        """
            Get all peaks with similar peaks distance for each spectrum in the spectrum list.
            This method run on the ana_res where the peaks with similar inter-peak distances are 
            identified already. The method here just to make sure one peak is only presented in one group,
            which is constraint by picking peak from the group with lower peak distance then ignore any
            group has overlapping peaks but with higher peak distance.

            Return a list of the grouped peaks and their average peak distance 
            
            Argument:
                ana_res: list of PeakAnalysisResult
                peaks: fitted peaks
                exclusive: not allow one peak position to be occupy many time if True
        """
        
        allpeaks_unfilterd = [ (res.peaks_family, res.avg_dist) for res in ana_res ]
        selected = np.repeat(False, len(peaks))
        allpeaks_unfilterd = sorted( allpeaks_unfilterd, key= lambda x: x[1] )

        out = []
        for family, avg_dist in allpeaks_unfilterd:
            if avg_dist < np.inf :
                if (not exclusive or np.all( selected[family] == False ) ):
                    out.append((family, avg_dist))
                    selected[family] = True
        out.append( ((np.arange(len(peaks))[~selected]).tolist(), -1 ) )
        return out


@dataclass
class CollapseSpectrum(Spectrum):
    sx: int # cropped starting point - x
    sy : int #  cropped starting point - y
    ex : int # cropped ending point - x
    ey : int # cropped ending point - y

    @classmethod
    def from_rheed_horizontal(cls, rd, sx, sy, ex, ey):
        """
            get horizontal collapse spectrum which is basically the integrated spectrum over rows
        """
        pattern = crop(rd.pattern, sx, sy, ex, ey)
        return cls(pattern.sum(axis=0), np.arange(sy, ey), sx, sy, ex, ey)

    @classmethod
    def from_rheed_vertical(cls, rd, sx, sy, ex, ey):
        """
            get vertical collapse spectrum which is basically the integrated spectrum over columns
        """

        pattern = crop(rd.pattern, sx, sy, ex, ey)
        return cls(pattern.sum(axis=1), np.arange(sx, ex), sx, sy, ex, ey)

# Add spectrum Model
# Gaussian, Lorenzien or Vogti fitter

class SpectrumModel:
    """
        Reference 
        1. https://chrisostrouchov.com/post/peak_fit_xrd_python/
        2. https://lmfit.github.io/lmfit-py/examples/documentation/builtinmodels_nistgauss2.html#sphx-glr-examples-documentation-builtinmodels-nistgauss2-py
    """
    
    _model_prefix = {
        "GaussianModel":"G{}_",
        "LorentzianModel":"L{}_",
        "VoigtModel":"V{}_"
    }
    
    def __init__(self, model, sub_models, params,):
        self.sub_models = sub_models # list of lmfit models
        self.model = model
        self.params = params
        self.output_fit = None
    
    @classmethod
    def from_peaks(cls, peaks, peaks_info, spec, config, bg_mask, by="guess"):
        composite_model = None
        sub_models = []
        params = None
        
        def _update(model, model_params, params, sub_models, composite_model):
            if isinstance(model_params, dict):
                model_params = model.make_params(**model_params)
            else:
                model_params = model.make_params(**params)
                
            if params is None:
                params = model_params
            else:
                params.update(model_params)
            # display(params)    
            if composite_model is None:
                composite_model = model
            else:
                composite_model = composite_model + model            
             
            sub_models.append(model)
        
            return params, sub_models, composite_model
        
        def _guess_FWHM(spec, peaks, peak_heights, config):
            window = config.peak_window
            wms = peak_heights / 2
            
            above_wm = [ spec.spec[max(p-window,0) : p+window ] < wm for p, wm in zip(peaks, wms) ]
            x = [ spec.ws[max(p-window,0) : p+window ] for p in peaks ]
            
            peak_lefts = []
            peak_rights = []
            for i in range(len(above_wm)):
                above_wm_int = np.where(~above_wm[i])[0]
                if len(above_wm_int)>0:
                    left = above_wm_int.min()
                    right = above_wm_int.max()
                else:
                    left = 0
                    right = len(x[i])-1
                    
                peak_lefts.append(x[i][left])
                peak_rights.append(x[i][right])
                
            peak_widths = np.array(peak_rights) - np.array(peak_lefts)
            return peak_widths
        
        def _default_params_from_peaks(type, prefix, p, p_w, p_h, config):
            if type == "GaussianModel":
                # default guess is horrible!! do not use guess()

                center = p
                
                sigma = p_w / 2.355
                amplitude = p_h * (sigma * np.sqrt(2*np.pi))

                default_params = {
                    f"{prefix}center": center,
                    f"{prefix}amplitude": amplitude,
                    f"{prefix}sigma": sigma
                }
            elif type == "LorentzianModel":
                center = p
                sigma = p_w / 2
                amplitude = p_h * (sigma * np.pi)

                default_params = {
                    f"{prefix}center": center,
                    f"{prefix}amplitude": amplitude,
                    f"{prefix}sigma": sigma
                }

            elif type == "VoigtModel":
                center = p
                sigma = p_w / 3.6013
                amplitude = p_h * (sigma * np.sqrt(2*np.pi))

                default_params = {
                    f"{prefix}center": center,
                    f"{prefix}amplitude": amplitude,
                    f"{prefix}sigma": sigma
                }
            else:
                raise NotImplementedError("Unknown type: {type}")

            return default_params        
        
        peak_heights = spec.spec[peaks]
        peak_xs = spec.ws[peaks]
        peak_widths = _guess_FWHM(spec, peaks, peak_heights, config)
        
        for i,(p, p_x, p_w, p_h,) in enumerate(zip(peaks, peak_xs, peak_widths, peak_heights)):
            p_x, p_w, p_h = float(p_x), float(p_w), float(p_h)
            try:
                if isinstance(config.type, str):
                    m_type = config.type
                    prefix = cls._model_prefix[m_type].format(i)
                    model = getattr(lm_models, m_type)(prefix=prefix)
                elif isinstance(config.type, type):
                    m_type = config.type.__name__
                    prefix = cls._model_prefix[m_type].format(i)
                    model = config.type(prefix=prefix)
            except KeyError as key_e:
                raise NotImplementedError(f'model {config.type} not implemented yet, {key_e}')
            except Exception as e:
                raise e
            
            model.set_param_hint('sigma', **config.sigma)
            model.set_param_hint('center', **config.center)
            model.set_param_hint('height', **config.height)
            model.set_param_hint('amplitude', **config.amplitude)
            
            if by == "guess":
                guess_params = model.guess(
                    spec.spec[p-config.peak_window:p+config.peak_window],
                    spec.ws[p-config.peak_window:p+config.peak_window]
                )
            
                params, sub_models, composite_model = _update(model,guess_params, params, sub_models, composite_model)
                
            else:
                default_params = _default_params_from_peaks(m_type, prefix, p_x, p_w, p_h, config)
                params, sub_models, composite_model = _update(model,default_params, params, sub_models, composite_model)

        
        # add lm_models.PolynomialModel() for background
        
        model = lm_models.PolynomialModel(degree=config.poly_n)
        guess_params = model.guess(spec.spec[bg_mask], spec.ws[bg_mask])

        params, sub_models, composite_model = _update(model, guess_params, params, sub_models, composite_model)
        
        return cls(composite_model, sub_models, params)    
        
    def modify_params(self, model_idx, **kargs):
        model = self.sub_models[model_idx]
        for param, options in kargs.items():
            model.set_param_hint(param, **options)
            
        params = model.make_params()
        self.params.update(params)
        
        return self
    
    def fit(self, spec, **kargs):
        output = self.model.fit(
            data = spec.spec,
            params= self.params,
            x = spec.ws,
            **kargs
        )
        self.output_fit = output
        return output
        
    def plot_component(self, spec, xlabel=None, ylabel=None, ax=None, **kargs):
        fig, ax = _create_figure(ax=ax, **kargs)
        ax.scatter(spec.ws, spec.spec, s=4)
        components = self.output_fit.eval_components(x=spec.ws)

        for k, v in components.items():
            ax.plot(spec.ws, v, label=k)
        if xlabel:
            ax.set_xlabel()
        if ylabel:
            ax.set_ylabel()
        ax.legend()

        return fig, ax

    def plot_fit(self, spec, **kargs):
        fig, gridspec = self.output_fit.plot(data_kws={'markersize': 1}, **kargs)
        fig.axes[0].title.set_text("Fit and Residual")
        return fig, gridspec