#
#  Copyright © Sorbonne University.
#
#  This source code is licensed under the MIT license found in the
#  LICENSE file in the root directory of this source tree.
#

import copy
import os
import sys
import numpy as np
from typing import Callable, List
sys.path.append("./src")

import hydra
import optuna
from omegaconf import DictConfig
from moviepy.editor import ipython_display as video_display

# %%
import torch
import torch.nn as nn

# %%
import gymnasium as gym
from gymnasium import Env
from gymnasium.wrappers import AutoResetWrapper

# %%
from bbrl import get_arguments, get_class
from bbrl.agents import TemporalAgent, Agents, PrintAgent
from bbrl.workspace import Workspace

from bbrl_algos.models.exploration_agents import EGreedyActionSelector
from bbrl_algos.models.critics import DiscreteQAgent
from bbrl_algos.models.loggers import Logger
from bbrl_algos.models.utils import save_best

from bbrl.visu.plot_critics import plot_discrete_q, plot_critic
from bbrl_algos.models.hyper_params import launch_optuna

from bbrl.utils.functional import gae
from bbrl.utils.chrono import Chrono

from bbrl.utils.replay_buffer import ReplayBuffer

# HYDRA_FULL_ERROR = 1
import matplotlib
import matplotlib.pyplot as plt

from bbrl_gymnasium.envs.maze_mdp import MazeMDPEnv
from bbrl_algos.wrappers.env_wrappers import MazeMDPContinuousWrapper
from bbrl.agents.gymnasium import make_env, ParallelGymAgent, record_video
from functools import partial


matplotlib.use("TkAgg")


def local_get_env_agents(cfg):
    eval_env_agent = ParallelGymAgent(
        partial(
            make_env,
            cfg.gym_env.env_name,
            autoreset=False,
        ),
        cfg.algorithm.nb_evals,
        include_last_state=True,
        seed=cfg.algorithm.seed.eval,
    )
    train_env_agent = ParallelGymAgent(
        partial(
            make_env,
            cfg.gym_env.env_name,
            autoreset=True,
        ),
        cfg.algorithm.n_envs,
        include_last_state=True,
        seed=cfg.algorithm.seed.train,
    )
    return train_env_agent, eval_env_agent


# %%
def compute_critic_loss(
    discount_factor, reward, must_bootstrap, action, q_values, q_target=None
):
    """Compute critic loss
    Args:
        discount_factor (float): The discount factor
        reward (torch.Tensor): a (2 × T × B) tensor containing the rewards
        must_bootstrap (torch.Tensor): a (2 × T × B) tensor containing 0 if the episode is completed at time $t$
        action (torch.LongTensor): a (2 × T) long tensor containing the chosen action
        q_values (torch.Tensor): a (2 × T × B × A) tensor containing Q values
        q_target (torch.Tensor, optional): a (2 × T × B × A) tensor containing target Q values

    Returns:
        torch.Scalar: The loss
    """
    if q_target is None:
        q_target = q_values
    argm = q_target.argmax(dim=-1)
    argm[0].detach()
    q = torch.gather(q_target[1],dim=-1,index=argm[0].unsqueeze(dim=-1))
    q = q.squeeze()
    target = reward[1] + discount_factor * q * must_bootstrap[1].float()
    #print("argm:",argm,"\nq:",q,"\ntarget:",target)

    qvals = torch.gather(q_values[0], dim=1, index=action[0].unsqueeze(dim=-1))
    qvals = qvals.squeeze(dim=1)


    # Compute critic loss
    mse = nn.MSELoss()
    critic_loss = mse(target, qvals)
    return critic_loss


# %%
def create_dqn_agent(cfg_algo, train_env_agent, eval_env_agent):
    # obs_space = train_env_agent.get_observation_space()
    # obs_shape = obs_space.shape if len(obs_space.shape) > 0 else obs_space.n

    # act_space = train_env_agent.get_action_space()
    # act_shape = act_space.shape if len(act_space.shape) > 0 else act_space.n

    state_dim, action_dim = train_env_agent.get_obs_and_actions_sizes()
    print(cfg_algo.architecture.hidden_sizes)

    critic = DiscreteQAgent(
        state_dim=state_dim,
        hidden_layers=list(cfg_algo.architecture.hidden_sizes),
        action_dim=action_dim,
        seed=cfg_algo.seed.q,
    )
    target_critic = copy.deepcopy(critic)

    explorer = EGreedyActionSelector(
        name="action_selector",
        epsilon=cfg_algo.explorer.epsilon_start,
        epsilon_end=cfg_algo.explorer.epsilon_end,
        epsilon_decay=cfg_algo.explorer.decay,
        seed=cfg_algo.seed.explorer,
    )
    q_agent = TemporalAgent(critic)
    
    target_q_agent = TemporalAgent(target_critic)

    tr_agent = Agents(train_env_agent, critic, explorer)  # , PrintAgent())
    ev_agent = Agents(eval_env_agent, critic)

    # Get an agent that is executed on a complete workspace
    train_agent = TemporalAgent(tr_agent)
    eval_agent = TemporalAgent(ev_agent)

    return train_agent, eval_agent, q_agent, target_q_agent


# %%
# Configure the optimizer over the q agent
def setup_optimizer(optimizer_cfg, q_agent):
    optimizer_args = get_arguments(optimizer_cfg)
    parameters = q_agent.parameters()
    optimizer = get_class(optimizer_cfg)(parameters, **optimizer_args)
    return optimizer


# %%
def run_ddqn(cfg, logger, trial=None):
    best_reward = float("-inf")
    if cfg.collect_stats:
        directory = "./ddqn_data/"
        if not os.path.exists(directory):
            os.makedirs(directory)
        filename = directory + "ddqn_" + cfg.gym_env.env_name + ".txt"
        fo = open(filename, "wb")
        stats_data = []

    # 1) Create the environment agent
    train_env_agent, eval_env_agent = local_get_env_agents(cfg)
    print(train_env_agent.envs[0])
    print(eval_env_agent.envs[0])

    # 2) Create the DQN-like Agent
    train_agent, eval_agent, q_agent, target_q_agent = create_dqn_agent(
        cfg.algorithm, train_env_agent, eval_env_agent
    )

    # 3) Create the training workspace
    train_workspace = Workspace()  # Used for training
    rb = ReplayBuffer(max_size=cfg.algorithm.buffer.max_size)
    # 5) Configure the optimizer
    optimizer = setup_optimizer(cfg.optimizer, q_agent)

    # 6) Define the steps counters
    nb_steps = 0
    tmp_steps_eval = 0
    last_critic_update_step = 0

    while nb_steps < cfg.algorithm.n_steps:
        # Decay the explorer epsilon
        explorer = train_agent.agent.get_by_name("action_selector")
        assert len(explorer) == 1, "There should be only one explorer"
        explorer[0].decay()

        # Execute the agent in the workspace
        if nb_steps > 0:
            train_workspace.zero_grad()
            train_workspace.copy_n_last_steps(1)
            train_agent(
                train_workspace,
                t=1,
                n_steps=cfg.algorithm.n_steps_train - 1,
            )
        else:
            train_agent(
                train_workspace,
                t=0,
                n_steps=cfg.algorithm.n_steps_train,
            )


        transition_workspace = train_workspace.get_transitions()

        action = transition_workspace["action"]
        nb_steps += action[0].shape[0]

        # Adds the transitions to the workspace
        rb.put(transition_workspace)
        if rb.size() > cfg.algorithm.buffer.learning_starts: # tant que le replay buffer n'est pas assez rempli, on continue de collecter des données dedans
            for _ in range(cfg.algorithm.optim_n_updates):
                rb_workspace = rb.get_shuffled(cfg.algorithm.buffer.batch_size)

                # The q agent needs to be executed on the rb_workspace workspace (gradients are removed in workspace)
                q_agent(rb_workspace, t=0, n_steps=2, choose_action=False)
                q_values, terminated, reward, action = rb_workspace[
                    "critic/q_values", "env/terminated", "env/reward", "action"
                ]

                with torch.no_grad():
                    target_q_agent(rb_workspace, t=0, n_steps=2, stochastic=True)
                target_q_values = rb_workspace["critic/q_values"]

                # Determines whether values of the critic should be propagated
                must_bootstrap = ~terminated[1]

                # Compute critic loss
                # FIXME: homogénéiser les notations (soit tranche temporelle, soit rien)
                critic_loss = compute_critic_loss(
                    cfg.algorithm.discount_factor, reward, must_bootstrap, action, q_values, target_q_values
                )
                # Store the loss for tensorboard display
                logger.add_log("critic_loss", critic_loss, nb_steps)

                optimizer.zero_grad()
                critic_loss.backward()
                torch.nn.utils.clip_grad_norm_(q_agent.parameters(), cfg.algorithm.max_grad_norm)
                optimizer.step()
                if nb_steps - last_critic_update_step > cfg.algorithm.target_critic_update_interval:
                    last_critic_update_step = nb_steps
                    target_q_agent.agent = copy.deepcopy(q_agent.agent)

        # Evaluate the agent
        if nb_steps - tmp_steps_eval > cfg.algorithm.eval_interval:
            tmp_steps_eval = nb_steps
            eval_workspace = Workspace()  # Used for evaluation
            eval_agent(
                eval_workspace,
                t=0,
                stop_variable="env/done",
                choose_action=True,
            )
            rewards = eval_workspace["env/cumulated_reward"][-1]
            logger.log_reward_losses(rewards, nb_steps)
            mean = rewards.mean()

            if mean > best_reward:
                best_reward = mean

            print(
                f"nb_steps: {nb_steps}, reward: {mean:.02f}, best: {best_reward:.02f}"
            )

            if trial is not None:
                trial.report(mean, nb_steps)
                if trial.should_prune():
                    raise optuna.TrialPruned()

            if cfg.save_best and best_reward == mean:
                save_best(
                    eval_agent,
                    cfg.gym_env.env_name,
                    best_reward,
                    "./dqn_best_agents/",
                    "dqn",
                )
                if cfg.plot_agents:
                    critic = eval_agent.agent.agents[1]
                    plot_discrete_q(
                        critic,
                        eval_env_agent,
                        best_reward,
                        "./dqn_plots/",
                        cfg.gym_env.env_name,
                        input_action="policy",
                    )
                    plot_discrete_q(
                        critic,
                        eval_env_agent,
                        best_reward,
                        "./dqn_plots2/",
                        cfg.gym_env.env_name,
                        input_action=None,
                    )
            if cfg.collect_stats:
                stats_data.append(mean)

            if trial is not None:
                trial.report(mean, nb_steps)
                if trial.should_prune():
                    raise optuna.TrialPruned()

    if cfg.collect_stats:
        # All rewards, dimensions (# of evaluations x # of episodes)
        stats_data = torch.stack(stats_data, axis=-1)
        print(np.shape(stats_data))
        np.savetxt(filename, stats_data.numpy())
        fo.flush()
        fo.close()

    if cfg.visualize:
        env = make_env(cfg.gym_env.env_name, render_mode="rgb_array")
        best_agent = copy.deepcopy(eval_agent.agent.agents[1])
        record_video(env, best_agent, "videos/ddqn.mp4")
        video_display("videos/ddqn.mp4")

    return best_reward


# %%
@hydra.main(
    config_path="configs/",
    # config_name="dqn_cartpole.yaml",
    config_name="ddqn_lunar_lander.yaml", 
    version_base="1.3")
def main(cfg_raw: DictConfig):
    torch.random.manual_seed(seed=cfg_raw.algorithm.seed.torch)

    if "optuna" in cfg_raw:
        launch_optuna(cfg_raw, run_ddqn)
    else:
        logger = Logger(cfg_raw)
        run_ddqn(cfg_raw, logger)


if __name__ == "__main__":
    main()