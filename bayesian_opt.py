"""BinBot v15.0 — Bayesian hyperparameter optimization

Replaces random-search in monitors.HyperOptimizer with sample-efficient
Bayesian optimization via Gaussian Process surrogate.

Falls back to random search if sklearn.gaussian_process not available.

Why this matters: random search wastes 80%+ of evaluations on bad regions.
Bayesian opt converges to the best params in ~10-20 evaluations vs 50+ for
random search. Cuts hyperopt time and finds better params.

Usage:
    from bayesian_opt import BayesianHyperOpt
    opt = BayesianHyperOpt(
        space={
            "rsi_buy":   (25, 42, "int"),
            "rsi_sell":  (58, 78, "int"),
            "bb_sd":     (1.5, 2.8, "float"),
        },
        objective_fn=lambda params: backtest_pnl(params),
        n_calls=20,
    )
    best_params, best_score = opt.run()
"""
from __future__ import annotations
import logging, random
from typing import Callable, Dict, List, Tuple, Any

log = logging.getLogger("binbot")

try:
    import numpy as np
    from sklearn.gaussian_process import GaussianProcessRegressor
    from sklearn.gaussian_process.kernels import Matern
    GP_AVAILABLE = True
except Exception:
    GP_AVAILABLE = False


class BayesianHyperOpt:
    """Gaussian-process Bayesian optimization. Maximizes objective."""

    def __init__(self, space: Dict[str, Tuple], objective_fn: Callable,
                 n_calls: int = 20, n_initial: int = 5, random_state: int = 42):
        self.space = space
        self.objective_fn = objective_fn
        self.n_calls = max(n_calls, n_initial + 1)
        self.n_initial = n_initial
        self.rng = random.Random(random_state)
        if GP_AVAILABLE:
            self._np = np
            self._np.random.seed(random_state)

    def _sample_random(self) -> Dict[str, Any]:
        out = {}
        for k, spec in self.space.items():
            lo, hi, kind = spec
            if kind == "int":
                out[k] = self.rng.randint(int(lo), int(hi))
            else:
                out[k] = round(self.rng.uniform(float(lo), float(hi)), 3)
        return out

    def _params_to_vec(self, params: Dict[str, Any]) -> List[float]:
        # Normalize each dim to [0, 1] so GP kernel treats them comparably
        v = []
        for k, spec in self.space.items():
            lo, hi, _ = spec
            x = float(params[k])
            v.append((x - lo) / (hi - lo) if hi > lo else 0.5)
        return v

    def _ucb_acquisition(self, gp, X_obs, kappa: float = 2.0):
        """Upper Confidence Bound: pick next point maximizing mu + kappa*sigma."""
        # Sample 500 candidate points, pick best by UCB
        n_dims = len(self.space)
        candidates = self._np.random.uniform(0, 1, size=(500, n_dims))
        mu, sigma = gp.predict(candidates, return_std=True)
        ucb = mu + kappa * sigma
        best_idx = int(self._np.argmax(ucb))
        norm_vec = candidates[best_idx]
        # Denormalize back to params
        out = {}
        for i, (k, spec) in enumerate(self.space.items()):
            lo, hi, kind = spec
            val = lo + norm_vec[i] * (hi - lo)
            out[k] = int(round(val)) if kind == "int" else round(float(val), 3)
        return out

    def run(self) -> Tuple[Dict[str, Any], float]:
        X_obs, y_obs = [], []
        best_params = None
        best_score = float("-inf")

        # Initial random sampling
        for i in range(self.n_initial):
            p = self._sample_random()
            score = self._safe_eval(p)
            X_obs.append(self._params_to_vec(p))
            y_obs.append(score)
            if score > best_score:
                best_score, best_params = score, p
            log.debug(f"BayesOpt init #{i+1}: score={score:.4f} params={p}")

        if not GP_AVAILABLE:
            # Fallback: continue with random search
            for i in range(self.n_calls - self.n_initial):
                p = self._sample_random()
                score = self._safe_eval(p)
                if score > best_score:
                    best_score, best_params = score, p
            log.info(f"BayesOpt (random fallback): best={best_score:.4f} params={best_params}")
            return best_params or {}, best_score

        # Bayesian phase using GP-UCB
        kernel = Matern(nu=2.5)
        for i in range(self.n_calls - self.n_initial):
            try:
                gp = GaussianProcessRegressor(
                    kernel=kernel, n_restarts_optimizer=2,
                    normalize_y=True, alpha=1e-6, random_state=42
                )
                gp.fit(self._np.array(X_obs), self._np.array(y_obs))
                next_params = self._ucb_acquisition(gp, X_obs)
            except Exception as e:
                log.debug(f"GP fit failed, falling back to random: {e}")
                next_params = self._sample_random()
            score = self._safe_eval(next_params)
            X_obs.append(self._params_to_vec(next_params))
            y_obs.append(score)
            if score > best_score:
                best_score, best_params = score, next_params
            log.debug(f"BayesOpt #{i+1}: score={score:.4f} params={next_params}")

        log.info(f"🔬 BayesOpt complete: best_score={best_score:.4f} best_params={best_params}")
        return best_params or {}, best_score

    def _safe_eval(self, params: Dict[str, Any]) -> float:
        try:
            return float(self.objective_fn(params))
        except Exception as e:
            log.debug(f"BayesOpt objective_fn failed: {e}")
            return float("-inf")
