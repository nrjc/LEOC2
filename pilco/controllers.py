import math
from typing import Tuple, List

import tensorflow as tf
from tensorflow_probability import distributions as tfd, bijectors
import numpy as np
import gpflow
from gpflow import Parameter
from gpflow import set_trainable
from gpflow.utilities import positive
from tensorflow_probability.python.bijectors import Chain, ScaleMatvecDiag

f64 = gpflow.utilities.to_default_float

from .models import MGPR

float_type = gpflow.config.default_float()


def squash_sin(m, s, max_action=None):
    '''
    Squashing function, passing the controls mean and variance
    through a sinus, as in gSin.m. The output is in [-max_action, max_action].
    IN: mean (m) and variance(s) of the control input, max_action
    OUT: mean (M) variance (S) and input-output (C) covariance of the squashed
         control input
    '''
    k = tf.shape(m)[1]
    if max_action is None:
        max_action = tf.ones((1, k), dtype=float_type)  # squashes in [-1,1] by default
    else:
        max_action = max_action * tf.ones((1, k), dtype=float_type)

    M = max_action * tf.exp(-tf.linalg.diag_part(s) / 2) * tf.sin(m)

    lq = -(tf.linalg.diag_part(s)[:, None] + tf.linalg.diag_part(s)[None, :]) / 2
    q = tf.exp(lq)
    S = (tf.exp(lq + s) - q) * tf.cos(tf.transpose(m) - m) \
        - (tf.exp(lq - s) - q) * tf.cos(tf.transpose(m) + m)
    S = max_action * tf.transpose(max_action) * S / 2

    C = max_action * tf.linalg.diag(tf.exp(-tf.linalg.diag_part(s) / 2) * tf.cos(m))
    return M, S, tf.reshape(C, shape=[k, k])


class LinearController(gpflow.Module):
    def __init__(self, state_dim, control_dim, max_action=1.0, W=None, b=None):
        gpflow.Module.__init__(self)
        if W is None:
            self.W = Parameter(np.random.rand(control_dim, state_dim))
        else:
            self.W = Parameter(W)
        self.b = Parameter(np.zeros((1, control_dim), dtype=float_type))
        self.max_action = max_action

    def compute_action(self, m, s, squash=True):
        '''
        Simple affine action:  M <- W(m-t) - b
        IN: mean (m) and variance (s) of the state
        OUT: mean (M) and variance (S) of the action
        '''
        M = m @ tf.transpose(self.W) + self.b  # mean output
        S = self.W @ s @ tf.transpose(self.W)  # output variance
        V = tf.transpose(self.W)  # input output covariance
        if squash:
            M, S, V2 = squash_sin(M, S, self.max_action)
            V = V @ V2
        return M, S, V

    def randomize(self):
        mean = 0;
        sigma = 1
        self.W.assign(mean + sigma * np.random.normal(size=self.W.shape))
        self.b.assign(mean + sigma * np.random.normal(size=self.b.shape))


class FakeGPR(gpflow.Module):
    def __init__(self, data, kernel, X=None, likelihood_variance=1e-4):
        gpflow.Module.__init__(self)
        if X is None:
            self.X = Parameter(data[0], name="DataX", dtype=gpflow.default_float())
        else:
            self.X = X
        self.Y = Parameter(data[1], name="DataY", dtype=gpflow.default_float())
        self.data = [self.X, self.Y]
        self.kernel = kernel
        self.likelihood = gpflow.likelihoods.Gaussian()
        self.likelihood.variance.assign(likelihood_variance)
        set_trainable(self.likelihood.variance, False)


class RbfController(MGPR):
    '''
    An RBF Controller implemented as a deterministic GP
    See Deisenroth et al 2015: Gaussian Processes for Data-Efficient Learning in Robotics and Control
    Section 5.3.2.
    '''

    def __init__(self, state_dim, control_dim, num_basis_functions, max_action=1.0):
        MGPR.__init__(self,
                      [np.random.randn(num_basis_functions, state_dim),
                       0.1 * np.random.randn(num_basis_functions, control_dim)]
                      )
        for model in self.models:
            model.kernel.variance.assign(1.0)
            set_trainable(model.kernel.variance, False)
        self.max_action = max_action

    def create_models(self, data):
        self.models = []
        for i in range(self.num_outputs):
            kernel = gpflow.kernels.SquaredExponential(lengthscales=tf.ones([data[0].shape[1], ], dtype=float_type))
            transformed_lengthscales = Parameter(kernel.lengthscales, transform=positive(lower=1e-3))
            kernel.lengthscales = transformed_lengthscales
            kernel.lengthscales.prior = tfd.Gamma(f64(1.1), f64(1 / 10.0))
            if i == 0:
                self.models.append(FakeGPR((data[0], data[1][:, i:i + 1]), kernel))
            else:
                self.models.append(FakeGPR((data[0], data[1][:, i:i + 1]), kernel, self.models[-1].X))

    def compute_action(self, m, s, squash=True):
        '''
        RBF Controller. See Deisenroth's Thesis Section
        IN: mean (m) and variance (s) of the state
        OUT: mean (M) and variance (S) of the action
        '''
        with tf.name_scope("controller") as scope:
            iK, beta = self.calculate_factorizations()
            M, S, V = self.predict_given_factorizations(m, s, 0.0 * iK, beta)
            S = S - tf.linalg.diag(self.variance - 1e-6)
        if squash:
            M, S, V2 = squash_sin(M, S, self.max_action)
            V = V @ V2
        return M, S, V

    def randomize(self):
        print("Randomising controller")
        for m in self.models:
            m.X.assign(np.random.normal(size=m.data[0].shape))
            m.Y.assign(self.max_action / 10 * np.random.normal(size=m.data[1].shape))
            mean = 1;
            sigma = 0.1
            m.kernel.lengthscales.assign(mean + sigma * np.random.normal(size=m.kernel.lengthscales.shape))

    def linearize(self, loc: np.ndarray) -> List[Tuple[np.ndarray, int]]:
        """ Linearize the RBF controller about a certain point loc, and return the weight and bias for each of
        the output dimensions.

        Args:
            loc: point about which to linearize [state_dim, 1]

        Returns: Returns a list of tuples

        """
        total_rbfs = self.num_datapoints
        returnable_obj = []
        state_dim = self.num_dims
        bias_term_collector = 0
        weight_term_collector = 0
        for m in self.models:
            centers = m.data[0][:, :].numpy()
            for center in centers:
                var = m.kernel.lengthscales.numpy()
                temp = (loc - center).reshape(state_dim, 1)
                exp_term = m.kernel.variance.numpy() * np.exp(
                    -0.5 * (temp.T @ np.diag(1 / var) @ temp).item())  # Check that it is truly 1/var and not var
                differential_term = exp_term * (np.diag(1 / var) @ temp)
                bias_term_collector += (exp_term - differential_term.T @ center.reshape(state_dim, 1)).item()
                weight_term_collector += differential_term
            returnable_obj.append((weight_term_collector / total_rbfs, bias_term_collector / total_rbfs))
        return returnable_obj


class CombinedController(gpflow.Module):
    '''
    An RBF Controller implemented as a deterministic GP
    See Deisenroth et al 2015: Gaussian Processes for Data-Efficient Learning in Robotics and Control
    Section 5.3.2.
    '''

    def __init__(self, state_dim, control_dim, num_basis_functions, controller_location=None, max_action=None, W=None,
                 **kwargs):
        gpflow.Module.__init__(self)
        if controller_location is None:
            controller_location = np.zeros((1, state_dim), float_type)
        self.rbf_controller = RbfController(state_dim, control_dim, num_basis_functions, max_action)
        self.linear_controller = LinearController(state_dim, control_dim, max_action, W=W)
        self.a = Parameter(controller_location, trainable=False)
        self.S = Parameter(np.ones(state_dim, float_type), trainable=True, transform=positive())
        self.r = 1
        self.max_action = max_action

    def compute_ratio(self, x):
        '''
        Compute the ratio of the linear controller
        '''
        S = self.S.read_value()
        a = self.a.read_value()
        # S = tf.constant([1.0, 2.0, 0.0])
        # d = (x - a) @ tf.linalg.inv(tf.linalg.diag(S)) @ tf.transpose(x - a)
        d = (x - a) @ tf.linalg.diag(S) @ tf.transpose(x - a)
        # ratio = 1 / tf.pow(d + 1, 2)
        ratio = 1 / tf.pow(d + 1, 2)
        return ratio

    def compute_action(self, m, s, squash=True):
        '''
        RBF Controller. See Deisenroth's Thesis Section
        IN: mean (m) and variance (s) of the state
        OUT: mean (M) and variance (S) of the action
        '''
        self.r = self.compute_ratio(m)
        M1, S1, V1 = self.linear_controller.compute_action(m, s, False)
        M2, S2, V2 = self.rbf_controller.compute_action(m, s, False)
        M = self.r * M1 + (1 - self.r) * M2
        S = self.r * S1 + (1 - self.r) * S2 + self.r * (M1 - M) @ tf.transpose(M1 - M) + (1 - self.r) * (M2 - M) @ tf.transpose(M2 - M)
        V = self.r * V1 + (1 - self.r) * V2
        if squash:
            M, S, V2 = squash_sin(M, S, self.max_action)
            V = V @ V2
        return M, S, V

    def randomize(self):
        self.rbf_controller.randomize()
        # mean = 0
        # sigma = 1
        # self.S.assign(mean + sigma * np.absolute(np.random.normal(size=self.S.shape)))

    def get_S(self):
        return self.S
