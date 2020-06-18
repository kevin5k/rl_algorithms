import argparse
from collections import OrderedDict, deque
from typing import Dict, List, Tuple

import numpy as np
import torch

from rl_algorithms.common.distributed.abstract.worker import Worker
from rl_algorithms.common.networks.brain import Brain
from rl_algorithms.registry import WORKERS, build_loss
from rl_algorithms.utils.config import ConfigDict


@WORKERS.register_module
class DQNWorker(Worker):
    def __init__(
        self,
        rank: int,
        args: argparse.Namespace,
        env_info: ConfigDict,
        hyper_params: ConfigDict,
        backbone: ConfigDict,
        head: ConfigDict,
        state_dict: OrderedDict,
        device: str,
    ):
        Worker.__init__(self, rank, args, env_info, hyper_params, device)
        self.loss_fn = build_loss(self.hyper_params.loss_type)
        self.backbone_cfg = backbone
        self.head_cfg = head
        self.head_cfg.configs.state_size = self.env_info.observation_space.shape
        self.head_cfg.configs.output_size = self.env_info.action_space.n

        self.use_n_step = self.hyper_params.n_step > 1

        self.max_epsilon = self.hyper_params.max_epsilon
        self.min_epsilon = self.hyper_params.min_epsilon
        self.epsilon = self.hyper_params.max_epsilon

        self._init_networks(state_dict)

    # pylint: disable=attribute-defined-outside-init
    def _init_networks(self, state_dict: OrderedDict):
        self.dqn = Brain(self.backbone_cfg, self.head_cfg).to(self.device)
        self.dqn.load_state_dict(state_dict)

    def load_params(self, path: str):
        """Load model and optimizer parameters."""
        Worker.load_params(self, path)

        params = torch.load(path)
        self.dqn.load_state_dict(params["dqn_state_dict"])
        print("[INFO] loaded the model and optimizer from", path)

    def select_action(self, state: np.ndarray) -> np.ndarray:
        """Select an action from the input space."""
        # epsilon greedy policy
        # pylint: disable=comparison-with-callable
        if self.epsilon > np.random.random():
            selected_action = np.array(self.env.action_space.sample())
        else:
            state = self._preprocess_state(state, self.device)
            selected_action = self.dqn(state).argmax()
            selected_action = selected_action.detach().cpu().numpy()

        # Decay epsilon
        self.epsilon = max(
            self.epsilon
            - (self.max_epsilon - self.min_epsilon) * self.hyper_params.epsilon_decay,
            self.min_epsilon,
        )

        return selected_action

    def step(self, action: np.ndarray) -> Tuple[np.ndarray, np.float64, bool, dict]:
        """Take an action and return the response of the env."""
        next_state, reward, done, info = self.env.step(action)
        return next_state, reward, done, info

    def compute_priorities(self, memory: Dict[str, np.ndarray]) -> np.ndarray:
        """Compute initial priority values of experiences in local memory"""
        states = torch.FloatTensor(memory["states"]).to(self.device)
        actions = torch.LongTensor(memory["actions"]).to(self.device)
        rewards = torch.FloatTensor(memory["rewards"].reshape(-1, 1)).to(self.device)
        next_states = torch.FloatTensor(memory["next_states"]).to(self.device)
        dones = torch.FloatTensor(memory["dones"].reshape(-1, 1)).to(self.device)
        memory_tensors = (states, actions, rewards, next_states, dones)

        dq_loss_element_wise, _ = self.loss_fn(
            self.dqn, self.dqn, memory_tensors, self.hyper_params.gamma, self.head_cfg
        )
        loss_for_prior = dq_loss_element_wise.detach().cpu().numpy()
        new_priorities = loss_for_prior + self.hyper_params.per_eps

        return new_priorities

    def collect_data(self) -> dict:
        """Fill and return local buffer"""
        local_memory = [0]
        local_memory = dict(states=[], actions=[], rewards=[], next_states=[], dones=[])
        local_memory_keys = local_memory.keys()
        if self.use_n_step:
            nstep_queue = deque(maxlen=self.hyper_params.n_step)

        while len(local_memory["states"]) < self.hyper_params.local_buffer_max_size:
            state = self.env.reset()
            done = False
            score = 0
            num_steps = 0
            while not done:
                if self.args.worker_render:
                    self.env.render()
                num_steps += 1
                action = self.select_action(state)
                next_state, reward, done, _ = self.step(action)
                transition = (state, action, reward, next_state, int(done))
                if self.use_n_step:
                    nstep_queue.append(transition)
                    if self.hyper_params.n_step == len(nstep_queue):
                        nstep_exp = self.preprocess_nstep(nstep_queue)
                        for entry, keys in zip(nstep_exp, local_memory_keys):
                            local_memory[keys].append(entry)
                else:
                    for entry, keys in zip(transition, local_memory_keys):
                        local_memory[keys].append(entry)

                state = next_state
                score += reward

            print(score, num_steps, self.epsilon)

        for key in local_memory_keys:
            local_memory[key] = np.array(local_memory[key])

        return local_memory

    def synchronize(self, new_params: List[np.ndarray]):
        self._synchronize(self.dqn, new_params)
