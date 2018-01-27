# Copyright 2017 reinforce.io. All Rights Reserved.
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
# ==============================================================================

from __future__ import absolute_import
from __future__ import print_function
from __future__ import division

import tensorflow as tf

from tensorforce import util
from tensorforce.models import PGModel


class PGProbRatioModel(PGModel):
    """
    Policy gradient model based on computing likelihood ratios, e.g. TRPO and PPO.
    """

    def __init__(
        self,
        states,
        actions,
        scope,
        device,
        saver,
        summarizer,
        distributed,
        batching_capacity,
        variable_noise,
        states_preprocessing,
        actions_exploration,
        reward_preprocessing,
        update_mode,
        memory,
        optimizer,
        discount,
        network,
        distributions,
        entropy_regularization,
        baseline_mode,
        baseline,
        baseline_optimizer,
        gae_lambda,
        likelihood_ratio_clipping
    ):
        # Likelihood ratio clipping
        assert likelihood_ratio_clipping is None or likelihood_ratio_clipping > 0.0
        self.likelihood_ratio_clipping = likelihood_ratio_clipping

        self.reference = None
        self.compare = None

        super(PGProbRatioModel, self).__init__(
            states=states,
            actions=actions,
            scope=scope,
            device=device,
            saver=saver,
            summarizer=summarizer,
            distributed=distributed,
            batching_capacity=batching_capacity,
            variable_noise=variable_noise,
            states_preprocessing=states_preprocessing,
            actions_exploration=actions_exploration,
            reward_preprocessing=reward_preprocessing,
            update_mode=update_mode,
            memory=memory,
            optimizer=optimizer,
            discount=discount,
            network=network,
            distributions=distributions,
            entropy_regularization=entropy_regularization,
            baseline_mode=baseline_mode,
            baseline=baseline,
            baseline_optimizer=baseline_optimizer,
            gae_lambda=gae_lambda
        )

    def initialize(self, custom_getter):
        super(PGProbRatioModel, self).initialize(custom_getter)

        # Model comparison functions
        self.reference = tf.make_template(
            name_='reference',
            func_=self.tf_reference,
            custom_getter_=custom_getter
        )
        self.compare = tf.make_template(
            name_='compare',
            func_=self.tf_compare,
            custom_getter_=custom_getter
        )

    def tf_loss_per_instance(self, states, internals, actions, terminal, reward, next_states, next_internals, update):
        embedding = self.network.apply(x=states, internals=internals, update=update)
        prob_ratios = list()
        for name, distribution in self.distributions.items():
            distr_params = distribution.parameterize(x=embedding)
            log_prob = distribution.log_probability(distr_params=distr_params, action=actions[name])
            # works the same?
            # fixed_distr_params = tuple(tf.stop_gradient(input=x) for x in distr_params)
            # fixed_log_prob = distribution.log_probability(distr_params=fixed_distr_params, action=actions[name])
            fixed_log_prob = tf.stop_gradient(input=log_prob)
            prob_ratio = tf.exp(x=(log_prob - fixed_log_prob))
            collapsed_size = util.prod(util.shape(prob_ratio)[1:])
            prob_ratio = tf.reshape(tensor=prob_ratio, shape=(-1, collapsed_size))
            prob_ratios.append(prob_ratio)
        prob_ratio = tf.reduce_mean(input_tensor=tf.concat(values=prob_ratios, axis=1), axis=1)
        if self.likelihood_ratio_clipping is None:
            return -prob_ratio * reward
        else:
            clipped_prob_ratio = tf.clip_by_value(
                t=prob_ratio,
                clip_value_min=(1.0 / (1.0 + self.likelihood_ratio_clipping)),
                clip_value_max=(1.0 + self.likelihood_ratio_clipping)
            )
            return -tf.minimum(x=(prob_ratio * reward), y=(clipped_prob_ratio * reward))

    def tf_reference(self, states, internals, actions, terminal, reward, next_states, next_internals, update):
        embedding = self.network.apply(x=states, internals=internals, update=update)
        log_probs = list()
        for name in sorted(self.distributions):
            distribution = self.distributions[name]
            distr_params = distribution.parameterize(x=embedding)
            log_prob = distribution.log_probability(distr_params=distr_params, action=actions[name])
            collapsed_size = util.prod(util.shape(log_prob)[1:])
            log_prob = tf.reshape(tensor=log_prob, shape=(-1, collapsed_size))
            log_probs.append(log_prob)
        return tf.reduce_mean(input_tensor=tf.concat(values=log_probs, axis=1), axis=1)

    def tf_compare(self, reference, states, internals, actions, terminal, reward, next_states, next_internals, update):
        reward = self.fn_reward_estimation(
            states=states,
            internals=internals,
            terminal=terminal,
            reward=reward,
            update=update
        )
        embedding = self.network.apply(x=states, internals=internals, update=update)
        log_probs = list()
        for name in sorted(self.distributions):
            distribution = self.distributions[name]
            distr_params = distribution.parameterize(x=embedding)
            log_prob = distribution.log_probability(distr_params=distr_params, action=actions[name])
            collapsed_size = util.prod(util.shape(log_prob)[1:])
            log_prob = tf.reshape(tensor=log_prob, shape=(-1, collapsed_size))
            log_probs.append(log_prob)
        log_prob = tf.reduce_mean(input_tensor=tf.concat(values=log_probs, axis=1), axis=1)
        prob_ratio = tf.exp(x=(log_prob - reference))
        if self.likelihood_ratio_clipping is None:
            gain_per_instance = prob_ratio * reward
        else:
            clipped_prob_ratio = tf.clip_by_value(
                t=prob_ratio,
                clip_value_min=(1.0 / (1.0 + self.likelihood_ratio_clipping)),
                clip_value_max=(1.0 + self.likelihood_ratio_clipping)
            )
            gain_per_instance = tf.minimum(x=(prob_ratio * reward), y=(clipped_prob_ratio * reward))
        gain = tf.reduce_mean(input_tensor=gain_per_instance, axis=0)
        losses = self.fn_regularization_losses(states=states, internals=internals, update=update)
        if len(losses) > 0:
            gain -= tf.add_n(inputs=list(losses.values()))
        return gain

    def optimizer_arguments(self, states, actions, terminal, reward, internals, next_states, next_internals):
        arguments = super(PGProbRatioModel, self).optimizer_arguments(
            states=states,
            internals=internals,
            actions=actions,
            terminal=terminal,
            reward=reward,
            next_states=next_states,
            next_internals=next_internals
        )
        arguments['fn_reference'] = self.reference
        arguments['fn_compare'] = self.compare
        return arguments
