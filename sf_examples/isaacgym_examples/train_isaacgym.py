# this is here just to guarantee that isaacgym is imported before PyTorch
# isort: off
# noinspection PyUnresolvedReferences
import isaacgym

# isort: on

import os
import sys
from os.path import join
from typing import List

import gym
import torch
from isaacgymenvs.tasks import isaacgym_task_map
from isaacgymenvs.utils.reformat import omegaconf_to_dict
from torch import Tensor, nn

from sample_factory.cfg.arguments import parse_full_cfg, parse_sf_args
from sample_factory.envs.env_utils import register_env
from sample_factory.model.model_utils import EncoderBase, get_obs_shape, register_custom_encoder
from sample_factory.train import run_rl
from sample_factory.utils.typing import Config, Env
from sample_factory.utils.utils import str2bool


class IsaacGymVecEnv(gym.Env):
    def __init__(self, isaacgym_env, obs_key):
        self.env = isaacgym_env
        # what about vectorized multi-agent envs? should we take num_agents into account also?
        self.num_agents = self.env.num_envs
        self.action_space = self.env.action_space

        # isaacgym_examples environments actually return dicts
        if obs_key == "obs":
            self.observation_space = gym.spaces.Dict(dict(obs=self.env.observation_space))
            self._proc_obs_func = lambda obs_dict: obs_dict
        elif obs_key == "states":
            self.observation_space = gym.spaces.Dict(dict(obs=self.env.state_space))
            self._proc_obs_func = self._use_states_as_obs
        else:
            raise ValueError(f"Unknown observation key: {obs_key}")

    @staticmethod
    def _use_states_as_obs(obs_dict):
        obs_dict["obs"] = obs_dict["states"]
        return obs_dict

    def reset(self, *args, **kwargs):
        obs_dict = self.env.reset()
        # some IGE envs return all zeros on the first timestep, but this is probably okay
        return self._proc_obs_func(obs_dict)

    def step(self, actions):
        obs, rew, dones, infos = self.env.step(actions)
        return self._proc_obs_func(obs), rew, dones, infos

    def render(self, mode="human"):
        pass


def make_isaacgym_env(full_env_name: str, cfg: Config, _env_config=None) -> Env:
    task_name = full_env_name
    overrides = [f"task={task_name}"]
    if cfg.env_agents > 0:
        overrides.append(f"num_envs={cfg.env_agents}")

    import isaacgymenvs
    from hydra import compose, initialize

    # this will register resolvers for the hydra config
    # noinspection PyUnresolvedReferences
    from isaacgymenvs import train

    module_dir = isaacgymenvs.__path__[0]
    cfg_dir = join(module_dir, "cfg")
    curr_file_dir = os.path.dirname(os.path.abspath(__file__))
    cfg_dir = os.path.relpath(cfg_dir, curr_file_dir)
    initialize(config_path=cfg_dir, job_name="sf_isaacgym")
    ige_cfg = compose(config_name="config", overrides=overrides)

    rl_device = ige_cfg.rl_device
    sim_device = ige_cfg.sim_device
    graphics_device_id = ige_cfg.graphics_device_id

    ige_cfg_dict = omegaconf_to_dict(ige_cfg)
    task_cfg = ige_cfg_dict["task"]

    env = isaacgym_task_map[task_cfg["name"]](
        cfg=task_cfg,
        sim_device=sim_device,
        rl_device=rl_device,
        graphics_device_id=graphics_device_id,
        headless=cfg.env_headless,
        virtual_screen_capture=False,
        force_render=not cfg.env_headless,
    )

    env = IsaacGymVecEnv(env, cfg.obs_key)
    return env


class _IsaacGymMlpEncoderImlp(nn.Module):
    def __init__(self, obs_space, mlp_layers: List[int]):
        super().__init__()

        obs_shape = get_obs_shape(obs_space)
        assert len(obs_shape.obs) == 1

        layer_input_width = obs_shape.obs[0]
        encoder_layers = []
        for layer_width in mlp_layers:
            encoder_layers.append(nn.Linear(layer_input_width, layer_width))
            layer_input_width = layer_width
            encoder_layers.append(nn.ELU(inplace=True))

        self.mlp_head = nn.Sequential(*encoder_layers)

    def forward(self, obs: Tensor):
        x = self.mlp_head(obs)
        return x


class IsaacGymMlpEncoder(EncoderBase):
    def __init__(self, cfg, obs_space, timing):
        super().__init__(cfg, timing)

        self._impl = _IsaacGymMlpEncoderImlp(obs_space, cfg.mlp_layers)
        self._impl = torch.jit.script(self._impl)
        self.encoder_out_size = cfg.mlp_layers[-1]  # TODO: we should make this an abstract method

    def forward(self, obs_dict):
        x = self._impl(obs_dict["obs"])
        return x


def add_extra_params_func(parser):
    """
    Specify any additional command line arguments for this family of custom environments.
    """
    p = parser
    p.add_argument(
        "--env_agents",
        default=-1,
        type=int,
        help="Num agents in each env (default: -1, means use default value from isaacgymenvs env yaml config file)",
    )
    p.add_argument("--env_headless", default=True, type=str2bool, help="Headless == no rendering")
    p.add_argument(
        "--mlp_layers",
        default=[256, 128, 64],
        type=int,
        nargs="*",
        help="MLP layers to use with isaacgym_examples envs",
    )
    p.add_argument(
        "--obs_key",
        default="obs",
        type=str,
        help='IsaacGym envs return dicts, some envs return just "obs", and some return "obs" and "states".'
        "States key denotes the full state of the environment, and obs key corresponds to limited observations "
        'available in real world deployment. If we use "states" here we can train will full information '
        "(although the original idea was to use asymmetric training - critic sees full state and policy only sees obs).",
    )


# custom default configuration parameters for specific envs
# add more envs here analogously (env names should match config file names in IGE)
env_configs = dict(
    Ant=dict(
        mlp_layers=[256, 128, 64],
        experiment_summaries_interval=3,  # experiments are short so we should save summaries often
        save_every_sec=15,
        # trains better without normalized returns, but we keep the default value for consistency
        # normalize_returns=False,
    ),
    Humanoid=dict(
        train_for_env_steps=1310000000,  # to match how much it is trained in rl-games
        mlp_layers=[400, 200, 100],
        rollout=32,
        num_epochs=5,
        value_loss_coeff=4.0,
        max_grad_norm=1.0,
        num_batches_per_epoch=4,
        experiment_summaries_interval=3,  # experiments are short so we should save summaries often
        save_every_sec=15,
        # trains a lot better with higher gae_lambda, but we keep the default value for consistency
        # gae_lambda=0.99,
    ),
    AllegroHand=dict(
        train_for_env_steps=10_000_000_000,
        mlp_layers=[512, 256, 128],
        gamma=0.99,
        rollout=16,
        recurrence=16,
        use_rnn=False,
        learning_rate=5e-3,
        lr_schedule_kl_threshold=0.02,
        reward_scale=0.01,
        num_epochs=5,
        max_grad_norm=1.0,
        num_batches_per_epoch=8,
    ),
    AllegroHandLSTM=dict(
        train_for_env_steps=10_000_000_000,
        mlp_layers=[512, 256, 128],
        gamma=0.99,
        rollout=16,
        recurrence=16,
        use_rnn=True,
        learning_rate=1e-4,
        lr_schedule_kl_threshold=0.016,
        reward_scale=0.01,
        num_epochs=4,
        max_grad_norm=1.0,
        num_batches_per_epoch=8,
        obs_key="states",
    ),
)


def override_default_params_func(env, parser):
    """
    Override default argument values for this family of environments.
    All experiments for environments from my_custom_env_ family will have these parameters unless
    different values are passed from command line.

    """
    # most of these parameters are taken from IsaacGymEnvs default config files

    parser.set_defaults(
        # we're using a single very vectorized env, no need to parallelize it further
        batched_sampling=True,
        num_workers=1,
        num_envs_per_worker=1,
        worker_num_splits=1,
        actor_worker_gpus=[0],  # obviously need a GPU
        train_for_env_steps=10000000,
        encoder_custom="isaac_gym_mlp_encoder",
        use_rnn=False,
        adaptive_stddev=False,
        policy_initialization="torch_default",
        env_gpu_actions=True,
        reward_scale=0.01,
        rollout=16,
        max_grad_norm=0.0,
        batch_size=32768,
        num_batches_per_epoch=2,
        num_epochs=4,
        ppo_clip_ratio=0.2,
        value_loss_coeff=2.0,
        exploration_loss_coeff=0.0,
        nonlinearity="elu",
        learning_rate=3e-4,
        lr_schedule="kl_adaptive_epoch",
        lr_schedule_kl_threshold=0.008,
        shuffle_minibatches=False,
        gamma=0.99,
        gae_lambda=0.95,
        with_vtrace=False,
        recurrence=1,
        value_bootstrap=True,  # assuming reward from the last step in the episode can generally be ignored
        normalize_input=True,
        normalize_returns=True,  # does not improve results on all envs, but with return normalization we don't need to tune reward scale
        save_best_after=int(5e6),
        serial_mode=True,  # it makes sense to run isaacgym envs in serial mode since most of the parallelism comes from the env itself (although async mode works!)
        async_rl=False,
    )

    # override default config parameters for specific envs
    if env in env_configs:
        parser.set_defaults(**env_configs[env])


def register_isaacgym_custom_components():
    for env_name in env_configs:
        register_env(env_name, make_isaacgym_env)
    register_custom_encoder("isaac_gym_mlp_encoder", IsaacGymMlpEncoder)


def parse_isaacgym_cfg(evaluation=False):
    parser, partial_cfg = parse_sf_args(evaluation=evaluation)
    add_extra_params_func(parser)
    override_default_params_func(partial_cfg.env, parser)
    final_cfg = parse_full_cfg(parser)
    return final_cfg


def main():
    """Script entry point."""
    register_isaacgym_custom_components()
    cfg = parse_isaacgym_cfg()
    status = run_rl(cfg)
    return status


if __name__ == "__main__":
    sys.exit(main())
