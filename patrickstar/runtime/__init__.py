# Copyright (C) 2021 THL A29 Limited, a Tencent company.
# All rights reserved.
# Licensed under the BSD 3-Clause License (the "License"); you may
# not use this file except in compliance with the License. You may
# obtain a copy of the License at
# https://opensource.org/licenses/BSD-3-Clause
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" basis,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied. See the License for the specific language governing
# permissions and limitations under the License.
# See the AUTHORS file for names of contributors.

from utils import log_dist, logger
from .engine import PatrickStarEngine
from .init_context import Init


def initialize_engine(args=None,
                      model=None,
                      client=None,
                      optimizer=None,
                      model_parameters=None,
                      lr=0.01,
                      betas=(0.9, 0.999),
                      eps=1e-8,
                      weight_decay=0):
    """Initialize the PatrickStar Engine.
    Arguments:
        args: an object containing local_rank and deepspeed_config fields.
            This is optional if `config` is passed.
        model: Required: nn.module class before apply any wrappers
        optimizer: Optional: a user defined optimizer, this is typically used instead of defining
            an optimizer in the DeepSpeed json config.
        model_parameters: Optional: An iterable of torch.Tensors or dicts.
            Specifies what Tensors should be optimized.
        training_data: Optional: Dataset of type torch.utils.data.Dataset
        lr_scheduler: Optional: Learning Rate Scheduler Object. It should define a get_lr(),
            step(), state_dict(), and load_state_dict() methods
        mpu: Optional: A model parallelism unit object that implements
            get_{model,data}_parallel_{rank,group,world_size}()
        dist_init_required: Optional: None will auto-initialize torch.distributed if needed,
            otherwise the user can force it to be initialized or not via boolean.
        collate_fn: Optional: Merges a list of samples to form a
            mini-batch of Tensor(s).  Used when using batched loading from a
            map-style dataset.
        config: Optional: Instead of requiring args.deepspeed_config you can pass your deepspeed config
            as an argument instead, as a path or a dictionary.
        config_params: Optional: Same as `config`, kept for backwards compatibility.
    Returns:
        A tuple of ``engine``, ``optimizer``, ``training_dataloader``, ``lr_scheduler``
        * ``engine``: DeepSpeed runtime engine which wraps the client model for distributed training.
        * ``optimizer``: Wrapped optimizer if a user defined ``optimizer`` is supplied, or if
          optimizer is specified in json config else ``None``.
        * ``training_dataloader``: DeepSpeed dataloader if ``training_data`` was supplied,
          otherwise ``None``.
        * ``lr_scheduler``: Wrapped lr scheduler if user ``lr_scheduler`` is passed, or
          if ``lr_scheduler`` specified in JSON configuration. Otherwise ``None``.
    """
    log_dist("DeepSpeed info", ranks=[0])

    assert model is not None, "deepspeed.initialize requires a model"

    engine = PatrickStarEngine(args=args,
                               model=model,
                               client=client,
                               optimizer=optimizer,
                               model_parameters=model_parameters,
                               lr=lr,
                               betas=betas,
                               eps=eps,
                               weight_decay=weight_decay)

    return_items = [
        engine, engine.optimizer, engine.training_dataloader,
        engine.lr_scheduler
    ]
    return tuple(return_items)