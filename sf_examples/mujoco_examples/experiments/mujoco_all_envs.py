from sample_factory.runner.run_description import Experiment, ParamGrid, RunDescription

_params = ParamGrid(
    [
        ("seed", [0, 1111, 2222, 3333, 4444, 5555, 6666, 7777, 8888, 9999]),
        (
            "env",
            [
                "mujoco_ant",
                "mujoco_halfcheetah",
                "mujoco_hopper",
                "mujoco_humanoid",
                "mujoco_doublependulum",
                "mujoco_pendulum",
                "mujoco_reacher",
                "mujoco_swimmer",
                "mujoco_walker",
            ],
        ),
    ]
)

_experiments = [
    Experiment(
        "mujoco_all_envs",
        "python -m sf_examples.mujoco_examples.train_mujoco --algo=APPO --with_wandb=True --wandb_tags mujoco runner_4",
        _params.generate_params(randomize=False),
    ),
]


RUN_DESCRIPTION = RunDescription("mujoco_all_envs", experiments=_experiments)
# python -m sample_factory.runner.run --run=sf_examples.mujoco_examples.experiments.mujoco_all_envs --runner=processes --max_parallel=4  --pause_between=1 --experiments_per_gpu=10000 --num_gpus=1 --experiment_suffix=4
