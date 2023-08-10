import sys
import os
import copy
import torch
import torch.nn as nn
import gym
import my_gym
import hydra
import numpy as np

from omegaconf import DictConfig
from bbrl.utils.chrono import Chrono

from bbrl import get_arguments, get_class
from bbrl.workspace import Workspace
from bbrl.agents import Agents, TemporalAgent

from bbrl_algos.models.loggers import MyLogger, Logger
from bbrl.utils.replay_buffer import ReplayBuffer

from bbrl_algos.models.stochastic_actors import (
    SquashedGaussianActor,
    TunableVarianceContinuousActor,
    DiscreteActor,
)
from bbrl_algos.models.critics import ContinuousQAgent
from bbrl_algos.models.shared_models import soft_update_params
from bbrl_algos.models.envs import get_env_agents

from bbrl.visu.plot_policies import plot_policy
from bbrl.visu.plot_critics import plot_critic

# HYDRA_FULL_ERROR = 1

import matplotlib

matplotlib.use("TkAgg")


# Create the SAC Agent
def create_sac_agent(cfg, train_env_agent, eval_env_agent):
    obs_size, act_size = train_env_agent.get_obs_and_actions_sizes()
    assert (
        train_env_agent.is_continuous_action()
    ), "SAC code dedicated to continuous actions"
    actor = SquashedGaussianActor(
        obs_size, cfg.algorithm.architecture.actor_hidden_size, act_size
    )
    tr_agent = Agents(train_env_agent, actor)
    ev_agent = Agents(eval_env_agent, actor)
    critic_1 = ContinuousQAgent(
        obs_size, cfg.algorithm.architecture.critic_hidden_size, act_size
    )
    target_critic_1 = copy.deepcopy(critic_1)
    critic_2 = ContinuousQAgent(
        obs_size, cfg.algorithm.architecture.critic_hidden_size, act_size
    )
    target_critic_2 = copy.deepcopy(critic_2)
    train_agent = TemporalAgent(tr_agent)
    eval_agent = TemporalAgent(ev_agent)
    return (
        train_agent,
        eval_agent,
        actor,
        critic_1,
        target_critic_1,
        critic_2,
        target_critic_2,
    )


def make_gym_env(env_name):
    return gym.make(env_name)


# Configure the optimizer
def setup_optimizers(cfg, actor, critic_1, critic_2):
    actor_optimizer_args = get_arguments(cfg.actor_optimizer)
    parameters = actor.parameters()
    actor_optimizer = get_class(cfg.actor_optimizer)(parameters, **actor_optimizer_args)
    critic_optimizer_args = get_arguments(cfg.critic_optimizer)
    parameters = nn.Sequential(critic_1, critic_2).parameters()
    critic_optimizer = get_class(cfg.critic_optimizer)(
        parameters, **critic_optimizer_args
    )
    return actor_optimizer, critic_optimizer


def setup_entropy_optimizers(cfg):
    if cfg.algorithm.target_entropy == "auto":
        entropy_coef_optimizer_args = get_arguments(cfg.entropy_coef_optimizer)
        # Note: we optimize the log of the entropy coef which is slightly different from the paper
        # as discussed in https://github.com/rail-berkeley/softlearning/issues/37
        # Comment and code taken from the SB3 version of SAC
        log_entropy_coef = torch.log(
            torch.ones(1) * cfg.algorithm.entropy_coef
        ).requires_grad_(True)
        entropy_coef_optimizer = get_class(cfg.entropy_coef_optimizer)(
            [log_entropy_coef], **entropy_coef_optimizer_args
        )
    else:
        log_entropy_coef = 0
        entropy_coef_optimizer = None
    return entropy_coef_optimizer, log_entropy_coef


# %%
def compute_critic_loss(
    cfg, reward, must_bootstrap,
    t_actor, 
    q_agents, 
    target_q_agents, 
    rb_workspace,
    ent_coef
):
    """
    Computes the critic loss for a set of $S$ transition samples

    Args:
        cfg: The experimental configuration
        reward: Tensor (2xS) of rewards
        must_bootstrap: Tensor (S) of indicators
        t_actor: The actor agent (as a TemporalAgent)
        q_agents: The critics (as a TemporalAgent)
        target_q_agents: The target of the critics (as a TemporalAgent)
        rb_workspace: The transition workspace
        ent_coef: The entropy coefficient

    Returns:
        Tuple[torch.Tensor, torch.Tensor]: The two critic losses (scalars)
    """
                                                                                                                                                              
    # Compute q_values from both critics with the actions present in the buffer:
    # at t, we have Q(s,a) from the (s,a) in the RB
    q_agents(rb_workspace, t=0, n_steps=1)

    with torch.no_grad():
        # Replay the current actor on the replay buffer to get actions of the
        # current actor
        t_actor(rb_workspace, t=1, n_steps=1, stochastic=True)
        action_logprobs_next = rb_workspace["action_logprobs"]

        # Compute target q_values from both target critics: at t+1, we have
        # Q(s+1,a+1) from the (s+1,a+1) where a+1 has been replaced in the RB
        target_q_agents(rb_workspace, t=1, n_steps=1)

    q_values_rb_1, q_values_rb_2, post_q_values_1, post_q_values_2 = rb_workspace[
        "critic-1/q_value", "critic-2/q_value", "target-critic-1/q_value", "target-critic-2/q_value"
    ]

    # [[student]] Compute temporal difference

    q_next = torch.min(post_q_values_1[1], post_q_values_2[1]).squeeze(-1)
    v_phi = q_next - ent_coef * action_logprobs_next

    target = (
        reward[-1] + cfg.algorithm.discount_factor * v_phi * must_bootstrap.int()
    )
    td_1 = target - q_values_rb_1[0].squeeze(-1)
    td_2 = target - q_values_rb_2[0].squeeze(-1)
    td_error_1 = td_1**2
    td_error_2 = td_2**2
    critic_loss_1 = td_error_1.mean()
    critic_loss_2 = td_error_2.mean()
    # [[/student]]

    return critic_loss_1, critic_loss_2


# %%
def compute_actor_loss(ent_coef, t_actor, q_agents, rb_workspace):
    """
    Actor loss computation
    :param ent_coef: The entropy coefficient $\alpha$
    :param t_actor: The actor agent (temporal agent)
    :param q_agents: The critics (as temporal agent)
    :param rb_workspace: The replay buffer (2 time steps, $t$ and $t+1$)
    """
    
    # Recompute the q_values from the current actor, not from the actions in the buffer

    # [[student]] Recompute the action with the current actor (at $a_t$)
    t_actor(rb_workspace, t=0, n_steps=1, stochastic=True)
    action_logprobs_new = rb_workspace["action_logprobs"]
    # [[/student]]

    # [[student]] Compute Q-values
    q_agents(rb_workspace, t=0, n_steps=1)
    q_values_1, q_values_2 = rb_workspace["critic-1/q_value", "critic-2/q_value"]
    # [[/student]]
    current_q_values = torch.min(q_values_1, q_values_2).squeeze(-1)

    # [[student]] Compute the actor loss
    ## actor_loss =
    actor_loss = ent_coef * action_logprobs_new[0] - current_q_values[0]
    # [[/student]]

    return actor_loss.mean()


def run_sac(trial, cfg, logger):
    best_reward = float('-inf')
    ent_coef = cfg.algorithm.entropy_coef

    # 2) Create the environment agent
    train_env_agent, eval_env_agent = get_env_agents(cfg)
    
    # 3) Create the SAC Agent
    (
        train_agent,
        eval_agent,
        actor,
        critic_1,
        target_critic_1,
        critic_2,
        target_critic_2,
    ) = create_sac_agent(cfg, train_env_agent, eval_env_agent)

    t_actor = TemporalAgent(actor)
    q_agents = TemporalAgent(Agents(critic_1, critic_2))
    target_q_agents = TemporalAgent(Agents(target_critic_1, target_critic_2))
    train_workspace = Workspace()

    # Creates a replay buffer
    rb = ReplayBuffer(max_size=cfg.algorithm.buffer_size)

    # Configure the optimizer
    actor_optimizer, critic_optimizer = setup_optimizers(cfg, actor, critic_1, critic_2)
    entropy_coef_optimizer, log_entropy_coef = setup_entropy_optimizers(cfg)
    nb_steps = 0
    tmp_steps = 0

    # Initial value of the entropy coef alpha. If target_entropy is not auto,
    # will remain fixed
    if cfg.algorithm.target_entropy == "auto":
        target_entropy = -np.prod(train_env_agent.action_space.shape).astype(np.float32)
    else:
        target_entropy = cfg.algorithm.target_entropy

    # Training loop
    best_agent = actor
    for epoch in range(cfg.algorithm.max_epochs):
        # Execute the agent in the workspace
        if epoch > 0:
            train_workspace.zero_grad()
            train_workspace.copy_n_last_steps(1)
            train_agent(
                train_workspace,
                t=1,
                n_steps=cfg.algorithm.n_steps - 1,
                stochastic=True,
            )
        else:
            train_agent(
                train_workspace,
                t=0,
                n_steps=cfg.algorithm.n_steps,
                stochastic=True,
            )

        transition_workspace = train_workspace.get_transitions()
        action = transition_workspace["action"]
        nb_steps += action[0].shape[0]
        rb.put(transition_workspace)

        if nb_steps > cfg.algorithm.learning_starts:
            # Get a sample from the workspace
            rb_workspace = rb.get_shuffled(cfg.algorithm.batch_size)

            terminated, reward = rb_workspace[
                "env/terminated", "env/reward"
            ]
            ent_coef = torch.exp(log_entropy_coef.detach())

            # Critic update part ###############################
            critic_optimizer.zero_grad()

            (
                critic_loss_1, critic_loss_2
            ) = compute_critic_loss(
                cfg, 
                reward, 
                ~terminated[1],
                t_actor,
                q_agents,
                target_q_agents,
                rb_workspace,
                ent_coef
            )

            logger.add_log("critic_loss_1", critic_loss_1, nb_steps)
            logger.add_log("critic_loss_2", critic_loss_2, nb_steps)
            critic_loss = critic_loss_1 + critic_loss_2
            critic_loss.backward()
            torch.nn.utils.clip_grad_norm_(
                critic_1.parameters(), cfg.algorithm.max_grad_norm
            )
            torch.nn.utils.clip_grad_norm_(
                critic_2.parameters(), cfg.algorithm.max_grad_norm
            )
            critic_optimizer.step()


            # Actor update part ###############################
            actor_optimizer.zero_grad()
            actor_loss = compute_actor_loss(
                ent_coef, t_actor, q_agents, rb_workspace
            )
            logger.add_log("actor_loss", actor_loss, nb_steps)
            actor_loss.backward()
            torch.nn.utils.clip_grad_norm_(
                actor.parameters(), cfg.algorithm.max_grad_norm
            )
            actor_optimizer.step()

            # Entropy coef update part #####################################################
            if entropy_coef_optimizer is not None:
                # See Eq. (17) of the SAC and Applications paper
                # log. probs have been computed when computing
                # the actor loss
                action_logprobs_rb = rb_workspace[
                    "action_logprobs"
                ].detach()
                entropy_coef_loss = -(
                    log_entropy_coef.exp() * (action_logprobs_rb + target_entropy)
                ).mean()
                entropy_coef_optimizer.zero_grad()
                entropy_coef_loss.backward()
                entropy_coef_optimizer.step()
                logger.add_log("entropy_coef_loss", entropy_coef_loss, nb_steps)
                logger.add_log("entropy_coef", ent_coef, nb_steps)

            ####################################################

            # Soft update of target q function
            tau = cfg.algorithm.tau_target
            soft_update_params(critic_1, target_critic_1, tau)
            soft_update_params(critic_2, target_critic_2, tau)
            # soft_update_params(actor, target_actor, tau)

        # Evaluate ###########################################
        if nb_steps - tmp_steps > cfg.algorithm.eval_interval:
            tmp_steps = nb_steps
            eval_workspace = Workspace()  # Used for evaluation
            eval_agent(
                eval_workspace,
                t=0,
                stop_variable="env/done",
                stochastic=False,
            )
            rewards = eval_workspace["env/cumulated_reward"][-1]
            mean = rewards.mean()
            logger.add_log("reward/mean", mean, nb_steps)
            logger.add_log("reward/max", rewards.max(), nb_steps)
            logger.add_log("reward/min", rewards.min(), nb_steps)
            logger.add_log("reward/min", rewards.median(), nb_steps)
            print(f"nb steps: {nb_steps}, reward: {mean:.0f}, best: {best_reward:.0f}")
            if cfg.save_best and mean > best_reward:
                best_reward = mean
                directory = f"./agents/{cfg.gym_env.env_name}/sac_agent/"
                if not os.path.exists(directory):
                    os.makedirs(directory)
                filename = directory + "sac_" + str(mean.item()) + ".agt"
                best_filename = filename
                actor.save_model(filename)

    if best_filename:
        # Load agent from disk
        best_agent = torch.load(best_filename)
    return best_agent


@hydra.main(
    config_path="./configs/",
    # config_name="sac_cartpolecontinuous.yaml",
    config_name="sac_pendulum.yaml",
    # version_base="1.1",
)
def main(cfg: DictConfig):
    # print(OmegaConf.to_yaml(cfg))
    chrono = Chrono()
    torch.manual_seed(cfg.algorithm.seed)
    logger = Logger(cfg)
    run_sac(None, cfg, logger)
    chrono.stop()


if __name__ == "__main__":
    sys.path.append(os.getcwd())
    main()
