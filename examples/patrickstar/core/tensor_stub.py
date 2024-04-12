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

from patrickstar.core.const import AccessType, ParamType


class TensorInfo(object):
    r"""The info related to certain tensor."""

    def __init__(
        self,
        chunk_id: int,
        tensor_id: int,
        start_offset: int,
        numel: int,
        param: torch.nn.Parameter,
        access_type: AccessType,
        param_name="",
    ):
        self.tensor_id = tensor_id
        self.chunk_id = chunk_id
        self.start_offset = start_offset
        self.numel = numel
        self.param = param
        self.tensor_name = (
            f"{param_name}.data"
            if (access_type == AccessType.DATA)
            else f"{param_name}.grad"
        )
        self.access_type = access_type

    def __str__(self):
        return (
            f"tensor_id: {self.tensor_id}, name: {self.tensor_name}, "
            f"shape: {self.param.shape}, chunk_id: {self.chunk_id}, "
            f"start_offset: {self.start_offset}, numel: {self.numel}, state: {self.state()}"
        )

    def state(self):
        if self.param.ps_attr.param_type == ParamType.TORCH_BASED:
            return None
        else:
            return self.param.ps_attr.get_state(self.access_type)
