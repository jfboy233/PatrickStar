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

import math
import torch
import time
from pathlib import Path
from torch import Tensor
from typing import List, Optional
import logging
from client.const import PSTensorStatus, AccessType, TrainingStage
import utils.global_timer as global_timer
from utils import print_rank, logger, use_dist_flag, get_sys_memory_used
from client.parameter import register_param, is_torch_param
from deepspeed_helper.global_vars import get_args
from manager import PatrickStarManager
from client import ChunkList, ChunkTensorIndex
from .chunk_io_buff import FP32ChunkReadBuffer, FP16ChunkWriteBuffer


def get_real_data_tensor(param):
    if is_torch_param(param):
        return param
    else:
        return param.ps_attr.access_tensor(AccessType.DATA)


def FP16_f_adamv2(client,
                  fp32_params: List[torch.nn.Parameter],
                  fp16_param_with_grad_list,
                  exp_avgs: List[torch.nn.Parameter],
                  exp_avg_sqs: List[torch.nn.Parameter],
                  max_exp_avg_sqs: List[Tensor],
                  state_steps: List[int],
                  amsgrad: bool,
                  beta1_list: List[float],
                  beta2_list: List[float],
                  lr_list: List[float],
                  weight_decay_list: List[float],
                  eps_list: List[float],
                  prefer_device,
                  read_chunk_buff,
                  write_chunk_buff,
                  time_profile=True):
    r"""Functional API that performs Adam algorithm computation.
    按照在chunk内的存储顺序连续访问fp16_param_with_grad_list的参数，获取fp16 grad，
    以chunk为单位拷贝到一个tmp buff之中
    """
    if time_profile:
        adam_start_time = time.time()
    rank = torch.distributed.get_rank()
    world_size = torch.distributed.get_world_size()

    use_write_buff = True
    use_read_buff = True
    for i, param in enumerate(fp32_params):
        ##########################
        ####### 准备ADAM数据 ######
        ##########################
        if time_profile:
            adam_iter_access_start = time.time()

        # logging.info(f'fp16 cpu adam for param {i}')
        compute_device = prefer_device
        client.access_data(param, compute_device)
        param_data = get_real_data_tensor(param)

        fp16_param = fp16_param_with_grad_list[i]
        if time_profile:
            start_time = time.time()
        if use_read_buff:
            # 以chunk为粒度拷贝grad fp16 (FWD+BWD计算设备CUDA) -> grad fp32 (Adam计算设备CPU)
            if is_torch_param(fp16_param):
                param_grad = fp16_param.float(
                )  #torch.zeros_like(fp16_param, dtype = torch.float)
            else:
                # 将FP16 GPU Chunk拷贝到compute_device的FP16 Chunk上。
                # 如果是第一个tensor则拷贝Chunk，否则索引chunk
                param_grad = read_chunk_buff.access_from_cache(
                    fp16_param).view(param_data.shape)
        else:
            # 以tensor为粒度拷贝grad fp16 -> grad fp32
            client.access_data(fp16_param, torch.device(f'cuda:{client.rank}'))
            fp16_grad_tensor = get_real_data_tensor(fp16_param)

            if is_torch_param(fp16_param):
                param_grad = fp16_param.float()
            else:
                param_grad = torch.zeros(fp16_param.ps_attr.ps_shape,
                                         dtype=torch.float)
                param_grad.copy_(fp16_grad_tensor.view(
                    fp16_param.ps_attr.ps_shape),
                                 non_blocking=False)
        # # 必须释放fp16_param的内存，以便复用
        # client.release_data(fp16_param, PSTensorStatus.FREE)

        if time_profile:
            global_timer.gpu_cpu_move_elapse += time.time() - start_time
            global_timer.gpu_cpu_move_times += 1
            global_timer.gpu_cpu_move_data_amount += param_grad.numel()

        exp_avg_param = exp_avgs[i]
        exp_avg_sq_param = exp_avg_sqs[i]

        client.access_data(exp_avg_param, compute_device)
        client.access_data(exp_avg_sq_param, compute_device)

        exp_avg = get_real_data_tensor(exp_avg_param)
        exp_avg_sq = get_real_data_tensor(exp_avg_sq_param)

        ##########################
        ####### 开始ADAM计算 ######
        ##########################
        if time_profile:
            global_timer.cpu_adam_access_elapse += time.time(
            ) - adam_iter_access_start
            f_adam_compute_start_time = time.time()

        step = state_steps[i]
        beta1 = beta1_list[i]
        beta2 = beta2_list[i]
        eps = eps_list[i]

        bias_correction1 = 1 - beta1**step
        bias_correction2 = 1 - beta2**step

        weight_decay = weight_decay_list[i]

        if weight_decay != 0:
            param_grad = param_grad.add(param_data, alpha=weight_decay)

        exp_avg.mul_(beta1).add_(param_grad, alpha=1 - beta1)
        exp_avg_sq.mul_(beta2).addcmul_(param_grad,
                                        param_grad,
                                        value=1 - beta2)
        if amsgrad:
            # Maintains the maximum of all 2nd moment running avg. till now
            torch.maximum(max_exp_avg_sqs[i],
                          exp_avg_sq,
                          out=max_exp_avg_sqs[i])
            # Use the max. for normalizing running avg. of gradient
            denom = (max_exp_avg_sqs[i].sqrt() /
                     math.sqrt(bias_correction2)).add_(eps)
        else:
            denom = (exp_avg_sq.sqrt() / math.sqrt(bias_correction2)).add_(eps)

        lr = lr_list[i]
        step_size = lr / bias_correction1

        param_data.addcdiv_(exp_avg, denom, value=-step_size)

        if time_profile:
            global_timer.cpu_adam_f_elapse += time.time(
            ) - f_adam_compute_start_time
            adam_iter_release_start = time.time()

        mgr = PatrickStarManager()
        mgr.tiktac(client)
        ##########################
        ####### 结束ADAM计算 ######
        ##########################

        # fp16_param = fp16_param_with_grad_list[i]

        if time_profile:
            start_time = time.time()

        # TODO(jiaruifang) param_data(在compute_device上) -> fp16_param对应的Chunk内存
        if use_write_buff:
            write_chunk_buff.write_from_cache(fp16_param, param_data)
        else:
            # fp16_param先弄到GPU上，然后把fp16_data拷贝过去
            client.access_data(fp16_param, torch.device(f'cuda:{client.rank}'))
            fp16_data = get_real_data_tensor(fp16_param)
            fp16_data.copy_(param_data, non_blocking=False)
            client.release_data(fp16_param, PSTensorStatus.HOLD)

        if time_profile:
            global_timer.cpu_gpu_move_elapse += time.time() - start_time
            global_timer.cpu_gpu_move_data_amount += param_data.numel()
            global_timer.cpu_gpu_move_times += 1

        client.release_data(param)
        client.release_data(exp_avg_param)
        client.release_data(exp_avg_sq_param)

        if time_profile:
            global_timer.cpu_adam_release_elapse += time.time(
            ) - adam_iter_release_start

        mgr = PatrickStarManager()
        mgr.tiktac(client)
    if use_write_buff:
        write_chunk_buff.write_cached_chunk()
    global_timer.cpu_adam_elapse += time.time() - adam_start_time


class FP16Adam(torch.optim.Optimizer):
    def __init__(self,
                 client,
                 params,
                 lr=1e-3,
                 betas=(0.9, 0.999),
                 eps=1e-8,
                 weight_decay=0,
                 amsgrad=False,
                 prefer_device=torch.device('cpu:0')):
        """
        父类Optimzer实现细节
        https://github.com/pytorch/pytorch/blob/c371542efc/torch/optim/optimizer.py
        需要在register_module之前调用？也许不用，只用param的地址
        TODO(jiaruifang) prefer_device应该是自适应的
        """
        if not 0.0 <= lr:
            raise ValueError("Invalid learning rate: {}".format(lr))
        if not 0.0 <= eps:
            raise ValueError("Invalid epsilon value: {}".format(eps))
        if not 0.0 <= betas[0] < 1.0:
            raise ValueError("Invalid beta parameter at index 0: {}".format(
                betas[0]))
        if not 0.0 <= betas[1] < 1.0:
            raise ValueError("Invalid beta parameter at index 1: {}".format(
                betas[1]))
        if not 0.0 <= weight_decay:
            raise ValueError(
                "Invalid weight_decay value: {}".format(weight_decay))
        defaults = dict(lr=lr,
                        betas=betas,
                        eps=eps,
                        weight_decay=weight_decay,
                        amsgrad=amsgrad)
        super(FP16Adam, self).__init__(params, defaults)
        self.client = client
        self.prefer_device = prefer_device

        # 将group参数放置到每个param内部，可以按照参数切分并行计算adam
        for group in self.param_groups:
            for p in group['params']:
                self.state[p]['betas'] = group['betas']
                self.state[p]['lr'] = group['lr']
                self.state[p]['weight_decay'] = group['weight_decay']
                self.state[p]['eps'] = group['eps']

        # 用作fp16 grad 存储的buffer
        self.read_chunk_buff = None

    def __setstate__(self, state):
        super(CPUAdam, self).__setstate__(state)
        for group in self.param_groups:
            group.setdefault('amsgrad', False)

    def show_param(self):
        """
        Debug使用，展示model目前参数状态
        """
        rank = torch.distributed.get_rank()
        for n, param in self.client.module.named_parameters():
            if self.client.is_local_tensor(param, AccessType.DATA):
                self.client.access_data(
                    param, torch.device(f'cuda:{self.client.rank}'))
                grad_tensor = param.ps_attr.access_tensor(AccessType.DATA)
                logger.info(f'rank {rank} param {n} \'s grad {grad_tensor}')
                # TODO reset to HOLD？
                self.client.release_data(param, PSTensorStatus.HOLD)

    @torch.no_grad()
    def step(self, closure=None):
        """Performs a single optimization step.

        Args:
            closure (callable, optional): A closure that reevaluates the model
                and returns the loss.
        """
        args = get_args()
        rank = torch.distributed.get_rank()
        for n, param in self.client.module.named_parameters():
            if is_torch_param(param) and param.grad is not None:
                param.data = param.grad
                param.grad = None
                world_size = torch.distributed.get_world_size()

                torch.distributed.all_reduce(param.data,
                                             op=torch.distributed.ReduceOp.SUM,
                                             group=self.client.cpu_comm_group,
                                             async_op=False)
                param.data /= world_size

                logger.info(
                    f'rank {rank} allreduce grad {param.ps_attr.ps_name}')
                continue
            if param.ps_attr.get_status(
                    AccessType.DATA) == PSTensorStatus.COMPUTE:
                logger.debug(
                    f'rank {rank} release param {n} from COMPUTE to HOLD_AFTER_BWD'
                )
                tmp_tensor = param.ps_attr.access_tensor(AccessType.DATA)
                tmp_tensor.copy_(param.grad)
                param.grad = None

                if torch.distributed.is_initialized():
                    if use_dist_flag:
                        self.client.release_dist(
                            param,
                            AccessType.DATA,
                            PSTensorStatus.HOLD_AFTER_BWD,
                            training_stage=TrainingStage.BWD,
                            is_allreduce=True)
                    else:
                        self.client.release(param, AccessType.DATA,
                                            PSTensorStatus.HOLD_AFTER_BWD,
                                            True)
                else:
                    self.client.release_data(param, PSTensorStatus.HOLD)
        mgr = PatrickStarManager()
        mgr._training_stage == TrainingStage.ADAM
        mgr.tiktac(self.client)

        loss = None
        if closure is not None:
            with torch.enable_grad():
                loss = closure()

        fp16_param_with_grad_list = []
        fp32_param_list = []
        exp_avgs = []
        exp_avg_sqs = []
        state_sums = []
        max_exp_avg_sqs = []
        state_steps = []
        beta1_list = []
        beta2_list = []
        weight_decay_list = []
        eps_list = []
        lr_list = []

        max_param_size = 0
        for i, group in enumerate(self.param_groups):
            for j, p in enumerate(group['params']):
                if p.requires_grad:
                    # update the steps for each param group update
                    state = self.state[p]
                    state['step'] += 1

                    # p不是torch param，且p属于remote chunk跳过
                    if use_dist_flag and not is_torch_param(
                            p) and not self.client.is_local_tensor(
                                p, AccessType.DATA):
                        continue

                    if is_torch_param(p):
                        max_param_size = max(p.numel(), max_param_size)

                    fp16_param_with_grad_list.append(p)

                    exp_avgs.append(state['exp_avg'])
                    exp_avg_sqs.append(state['exp_avg_sq'])
                    fp32_param_list.append(state['fp32_param_data'])
                    beta1, beta2 = state['betas']

                    beta1_list.append(beta1)
                    beta2_list.append(beta2)
                    lr_list.append(state['lr'])
                    weight_decay_list.append(state['weight_decay'])
                    eps_list.append(state['eps'])

                    # record the step after step update
                    state_steps.append(state['step'])
                else:
                    raise RuntimeError(f"tensor id {p.ps_attr.grad_id()}")

        if self.read_chunk_buff is None:
            max_chunk_size = self.client.chunk_list.max_chunk_size()
            self.read_chunk_buff = FP32ChunkReadBuffer(
                self.client.chunk_list, self.client.chunk_tensor_index,
                max_chunk_size, self.prefer_device)
            logging.info(
                f"Allocate fp32 Chunk Buffer of size {max_chunk_size/1e6} MB.")
            self.write_chunk_buff = FP16ChunkWriteBuffer(
                self.client.chunk_list, self.client.chunk_tensor_index,
                max_chunk_size, self.prefer_device)

        # self.client.chunk_tensor_index.visit_chunks(self.client.chunk_list)
        FP16_f_adamv2(self.client, fp32_param_list, fp16_param_with_grad_list,
                      exp_avgs, exp_avg_sqs, max_exp_avg_sqs, state_steps,
                      False, beta1_list, beta2_list, lr_list,
                      weight_decay_list, eps_list, self.prefer_device,
                      self.read_chunk_buff, self.write_chunk_buff)

        mgr = PatrickStarManager()
        mgr.reset_metronome()
        return loss
