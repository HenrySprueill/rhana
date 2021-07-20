import re
from dataclasses import dataclass
from pathlib import Path

import signal

import numpy as np

from lmfit.model import Parameters, Parameter, Model, update_param_vals
from lmfit import models as lm_models
from lmfit.model import save_modelresult, save_model, load_model

from rhana.utils import _create_figure
from rhana.utils import Timeout
from rhana.utils import load_pickle, save_pickle


def maximum_amplitude(spectrum):
    # intergral by trapezoidal rule
    return float(np.trapz(spectrum.spec, spectrum.ws))

class ChebyshevPolynomialModel(Model):
    r"""A polynomial model with up to 7 Parameters, specified by `degree`.
    .. math::
        Tn(cos(theta)) = cos(n*theta)
    with parameters `c0`, `c1`, ..., `c7`. The supplied `degree` will
    specify how many of these are actual variable parameters. This uses
    :numpydoc:`polynomial.chebyshev.chebval` for its calculation of the polynomial.
    """

    MAX_DEGREE = 7
    DEGREE_ERR = "degree must be an integer less than %d."

    valid_forms = (0, 1, 2, 3, 4, 5, 6, 7)


    def __init__(self, degree=7, independent_vars=['x'], prefix='',
                 nan_policy='raise', **kwargs):
        kwargs.update({'prefix': prefix, 'nan_policy': nan_policy,
                       'independent_vars': independent_vars})
        if 'form' in kwargs:
            degree = int(kwargs.pop('form'))
        if not isinstance(degree, int) or degree > self.MAX_DEGREE:
            raise TypeError(self.DEGREE_ERR % self.MAX_DEGREE)

        self.poly_degree = degree
        pnames = ['c%i' % (i) for i in range(degree + 1)]
        kwargs['param_names'] = pnames

        def cheby_poly(x, c0=0, c1=0, c2=0, c3=0, c4=0, c5=0, c6=0, c7=0):
            return np.polynomial.chebyshev.chebval(x, [c0,c1,c2,c3,c4,c5,c6,c7])

        super().__init__(cheby_poly, **kwargs)


    def guess(self, data, x=None, **kwargs):
        """Guess starting values for the parameters of a model.
            Parameters
            ----------
            data : array_like
                Array of data to use to guess parameter values.
            **kws : optional
                Additional keyword arguments, passed to model function.
            Returns
            -------
            params : Parameters
                Initial, guessed values for the parameters of a Model.
        """
        pars = self.make_params()
        if x is not None:
            out = np.polynomial.chebyshev.chebfit(x=x, y=data, deg=self.poly_degree)
            for i, coef in enumerate(out):
                pars['%sc%i' % (self.prefix, i)].set(value=coef)
        return update_param_vals(pars, self.prefix, **kwargs)


@dataclass
class SpectrumModelConfig:
    """
        all parameter with dict type follow this convention
        {"value":..., "vary":..., "min":..., "max":..., "expr":...}
        
        height : confine the height of the peak
        sigma : confine the width of the peak
        center : confine the location of the peak
        amplitude : confine the AOC of the peak
        type : coule be one of the "GaussianModel", "LorentzianModel", "VoigtModel"
        use_cheby_poly : use Chebyshev Polynomial instead of normal polynomial if True
        poly_n : the polynomial order of the background, range from 0 to 7
        poly_zero_init : do zero initialize on polynomial term
        peak_window : how many pixel around left and right side of the peak is used to guess the solution
        add_vogit_bg : add a vogit back ground peak
        vogit_bg_amp_ratio : deprecated
        center_search_width : the width of the search region for a peak to migrate during fiting
        
    """
    height : dict
    sigma : dict
    center : dict
    amplitude : dict
    type : list
    use_cheby_poly : bool
    poly_n : int
    poly_zero_init : bool
    peak_window : int
    add_vogit_bg : bool
    vogit_bg_amp_ratio : float
    center_search_width : float

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
    
    def __init__(self, model, sub_models, params, result=None):
        self.sub_models = sub_models # list of lmfit models
        self.model = model
        self.params = params
        self.result = result
    
    @classmethod
    def from_peaks(cls, peaks, peaks_info, spec, config:SpectrumModelConfig, bg_mask:np.ndarray, by:str="guess"):
        """
            peaks: peaks index
            peaks_info: properties of each peak
            spec: the spectrum to fit
            config: model's config that provide some guide on how the final model should looks like
            bg_mask: a binary mask that is one where it is considered as background and zero elsewhere
            by: method to initialize the peak model. Options are "guess" and "other"
        """

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
                amplitude = float(p_h * (sigma * np.sqrt(2*np.pi)))

                default_params = {
                    f"{prefix}center": center,
                    f"{prefix}amplitude": amplitude,
                    f"{prefix}sigma": sigma
                }
            elif type == "LorentzianModel":
                center = p
                sigma = p_w / 2
                amplitude = float(p_h * (sigma * np.pi))

                default_params = {
                    f"{prefix}center": center,
                    f"{prefix}amplitude": amplitude,
                    f"{prefix}sigma": sigma
                }

            elif type == "VoigtModel":
                center = p
                sigma = p_w / 3.6013
                amplitude = float(p_h * (sigma * np.sqrt(2*np.pi)))

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
            except Exception as e:
                raise NotImplementedError(f'model {config.type} not implemented yet. Error {e}')
            
            
            center = dict(config.center)
            if config.center_search_width is not None:
                center['min'] = p_x - config.center_search_width / 2
                center['max'] = p_x + config.center_search_width / 2
            
            model.set_param_hint('sigma', **config.sigma)
            model.set_param_hint('center', **center)
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
        
        if config.use_cheby_poly:
            model = lm_models.PolynomialModel(degree=config.poly_n)
        else:
            model = ChebyshevPolynomialModel(degree=config.poly_n)

        if config.poly_zero_init:
            guess_params = model.make_params(**{ f"c{i}" : 0 for i in range(config.poly_n+1)})
        else:
            guess_params = model.guess(spec.spec[bg_mask], spec.ws[bg_mask])

        params, sub_models, composite_model = _update(model, guess_params, params, sub_models, composite_model)
        
        # add vogit background? for liquid phase disentanglement -> big spread that fit nicely by vogit!!!
        if config.add_vogit_bg:
            model = lm_models.VoigtModel(prefix="Lb0_")
            
            model.set_param_hint('center', **config.center)
            model.set_param_hint('height', **config.height)
            model.set_param_hint('amplitude', **config.amplitude)
            
            # guess_params = model.guess(spec.spec[bg_mask], spec.ws[bg_mask])
            guess_params = model.guess(spec.spec, spec.ws)
            # guess_params['Lb0_amplitude'].value = config.amplitude['max'] * config.vogit_bg_amp_ratio
    
            params, sub_models, composite_model = _update(model, guess_params, params, sub_models, composite_model)
        
        return cls(composite_model, sub_models, params)    
        
    def modify_params(self, model_idx, **kargs):
        model = self.sub_models[model_idx]
        for param, options in kargs.items():
            model.set_param_hint(param, **options)
            
        params = model.make_params()
        self.params.update(params)
        
        return self
    
    def fit(self, spec, timeout=5, **kargs):
        def _fit():
            output = self.model.fit(
                data = spec.spec,
                params= self.params,
                x = spec.ws,
                **kargs
            )
            return output
        
        if timeout is not None:
            with Timeout(seconds=timeout):
                self.result = _fit()
        else:
            self.result = _fit()
        return self.result
    
    def plot_component(self, spec, xlabel=None, ylabel=None, ax=None, **kargs):
        fig, ax = _create_figure(ax=ax, **kargs)
        ax.scatter(spec.ws, spec.spec, s=4)
        components = self.result.eval_components(x=spec.ws)

        for k, v in components.items():
            ax.plot(spec.ws, v, label=k)
        if xlabel:
            ax.set_xlabel()
        if ylabel:
            ax.set_ylabel()
        ax.legend()

        return fig, ax

    def is_fit_fail(self, thres_err=100, thres_fwhm=1, no_rela_okay=False):
        # should look into relative err see below's cell of how to get it from result
        if self.has_no_rela_err(self.result):
            return not no_rela_okay
        else:
            h_err = self.has_high_rela_err(self.result, thres_err)
            h_fwhm = self.has_high_fwhm(self.result, thres_fwhm)
            return  h_err or h_fwhm

    @staticmethod
    def has_high_fwhm(peak_fit_res, thres=1):
        params = peak_fit_res.result.params
        for k, v in params.items():
            if re.search(r"[VGL]\d+_fwhm", k) is not None:
                if v.value is None:
                    return True
                elif v.value > thres:
                    return True
        return False
    
    @staticmethod
    def has_no_rela_err(peak_fit_res):
        if peak_fit_res is None: return True

        params = peak_fit_res.result.params
        for k, v in params.items():
            if v.stderr is None:
                return True
        return False

    @staticmethod
    def has_high_rela_err(peak_fit_res, thres=100):
        params = peak_fit_res.result.params
        for k, v in params.items():
            if v.stderr is None:
                pass # sometimes it would not compute the stderr
            else:
                relative_err = int(max(0, v.stderr / v.value * 100))
                if relative_err > thres:
                    return True
        return False

    def save(self, path): 
        path = Path(path)
        assert path.is_dir(), f"path {path} must be path"
        params_path = path/"params.pkl"
        model_path = path/"model.sav"        
        
        model = self.result.model
        save_model(model, str(model_path))
        
        params = self.result.params
        save_pickle(params, params_path)
        
    
    @classmethod
    def load(cls, path):
        path = Path(path)
        assert path.is_dir(), f"path {path} must be path"
        params_path = path/"params.pkl"
        model_path = path/"model.sav"        
        
        model = load_model(str(model_path))
        params = load_pickle(params_path)
        return cls(model, [], params)
    
    def plot_fit(self, spec, **kargs):
        fig, gridspec = self.result.plot(data_kws={'markersize': 1}, **kargs)
        fig.axes[0].title.set_text("Fit and Residual")
        return fig, gridspec
