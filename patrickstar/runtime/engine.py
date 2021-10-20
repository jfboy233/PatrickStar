# BSD 3-Clause License
#
# Copyright (C) 2021 THL A29 Limited, a Tencent company.  All rights reserved.
#
# Redistribution and use in source and binary forms, with or without modification,
# are permitted provided that the following conditions are met:
#
#  * Redistributions of source code must retain the above copyright notice, this
#    list of conditions and the following disclaimer.
#
#  * Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
#  * Neither the name of the psutil authors nor the names of its contributors
#    may be used to endorse or promote products derived from this software without
#    specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
# ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR
# ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON
# ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
# SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import torch

from patrickstar.core import ChunkStatus, TensorStatus, TrainingStage
from patrickstar.fp16 import LossScaler, DynamicLossScaler
from patrickstar.manager import PatrickStarManager
from patrickstar.ops import FP16Adam
from patrickstar.utils import logger, global_timer

from .checkpoint import state_dict, load_state_dict


class PatrickStarEngine(torch.nn.Module):
    r"""DeepSpeed engine for training."""

    def __init__(self, model, client, config):
        super(PatrickStarEngine, self).__init__()
        self.module = model
        self.module.train()

        self.client = client

        # default parameter for adam.
        default_optim_config = {
            "type": "Adam",
            "params": {
                "lr": 0.01,
                "betas": (0.9, 0.999),
                "eps": 1e-8,
                "weight_decay": 0,
                "use_hybrid_adam": True,
            },
        }

        if config is not None:
            # Optimizer configuration
            optim_config = config.get("optimizer", default_optim_config)
            optim_type = optim_config.get("type", default_optim_config["type"])
            if optim_type not in ["Adam", "AdamW"]:
                raise ValueError(
                    f"Only support Adam and AdamW at the moment. "
                    f"Get optimizer type {optim_type}"
                )
            optim_params = optim_config.get("params", default_optim_config["params"])
            for key, val in default_optim_config["params"].items():
                if key not in optim_params:
                    optim_params[key] = val

            # Loss scaler configuration
            if "fp16" not in config:
                self.loss_scaler = None
            else:
                loss_scale_config = config["fp16"]
                assert loss_scale_config["enabled"], "Must enable fp16 training."
                assert (
                    "loss_scale" in loss_scale_config
                ), "Must have `loss_scale` field set."
                loss_scale = loss_scale_config["loss_scale"]
                if loss_scale == 0:
                    logger.info("Use DynamicLossScaler")
                    self.loss_scaler = DynamicLossScaler(
                        init_scale=(
                            2 ** loss_scale_config.get("initial_scale_power", 16)
                        ),
                        scale_factor=loss_scale_config.get("hysteresis", 2),
                        scale_window=loss_scale_config.get("loss_scale_window", 2000),
                        min_scale=loss_scale_config.get("min_loss_scale", 1),
                    )
                else:
                    self.loss_scaler = LossScaler(loss_scale)

            # Gradient clipping configuration
            if "gradient_clipping" not in config:
                self.gradient_clipping = -1
            else:
                self.gradient_clipping = config["gradient_clipping"]
        else:
            optim_type = default_optim_config["type"]
            optim_params = default_optim_config["params"]
            self.loss_scaler = None
            self.gradient_clipping = -1

        self.optimizer = FP16Adam(
            self.client,
            self.module.parameters(),
            loss_scaler=self.loss_scaler,
            gradient_clipping=self.gradient_clipping,
            lr=optim_params["lr"],
            betas=optim_params["betas"],
            eps=optim_params["eps"],
            weight_decay=optim_params["weight_decay"],
            use_adamw=(optim_type == "AdamW"),
            use_hybrid_adam=optim_params["use_hybrid_adam"],
        )

        self.client.init(self.module, self.optimizer)
        logger.info("PatrickStarEngine initialized.")

    def _reset_before_forward(self):
        mgr = PatrickStarManager()
        mgr.reset_metronome()
        for param_fp16 in self.client.chunk_based_param_fp16:
            param_fp16.ps_attr.fwd_used_cnt = 0
        for _, chunk in self.client.chunk_list.generate_chunk():
            chunk.unused = 0

    def _set_status_after_forward(self):
        """
        After forward calculation, we need to reset the status of
        tensors from HOLD_AFTER_FWD to HOLD. Otherwise, chunks may be
        released accidentally when using gradient checkpointing.
        """
        for chunk_id, chunk in self.client.chunk_list.generate_chunk():
            if (
                chunk.get_status() == ChunkStatus.HOLD
                or chunk.get_status() == ChunkStatus.HOLD_AFTER_FWD
            ):
                chunk.set_unused()
                self.client.set_all_tensors_status_in_chunk(chunk_id, TensorStatus.HOLD)

    def forward(self, *inputs, **kwargs):
        r"""Execute forward propagation
        Arguments:
            *inputs: Variable length input list
            **kwargs: variable length keyword arguments
        """
        global_timer.my_timer.start_profile("FWD")
        mgr = PatrickStarManager()
        mgr.set_training_stage(TrainingStage.FWD)
        self._reset_before_forward()

        loss = self.module(*inputs, **kwargs)
        self._set_status_after_forward()
        global_timer.my_timer.finish_profile("FWD")
        return loss

    def backward(self, loss):
        r"""Execute backward pass on the loss
        Arguments:
            loss: Torch tensor on which to execute backward propagation
        """
        global_timer.my_timer.start_profile("BWD")
        mgr = PatrickStarManager()
        mgr.set_training_stage(TrainingStage.BWD)

        for param_fp16 in self.client.chunk_based_param_fp16:
            param_fp16.ps_attr.bwd_used_cnt = 0

        self.optimizer.zero_grad()
        if self.loss_scaler:
            self.loss_scaler.backward(loss)
        else:
            loss.backward()
        mgr.update_margin_mem()
        global_timer.my_timer.finish_profile("BWD")

    def state_dict(self, destination=None, prefix="", keep_vars=False):
        return state_dict(
            self,
            self.client,
            destination=destination,
            prefix=prefix,
            keep_vars=keep_vars,
        )

    def load_state_dict(self, state_dict, strict=False):
        return load_state_dict(self, self.client, state_dict=state_dict, strict=strict)
