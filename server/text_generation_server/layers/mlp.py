import torch
import math
from torch import nn
from torch.nn import functional as F
from typing import Optional, Tuple
from text_generation_server.layers import TensorParallelEmbedding, FastLinear
from text_generation_server.layers.tensor_parallel import TensorParallelHead
from text_generation_server.utils.speculate import get_speculate

SQRT2 = 2**0.5

class MLPSpeculatorLayerNorm(nn.Module):
    """
    A L2 normalization implementation
    ...
    Args
    ----
    normalized_shape : int
        Dimensionality of input data (size of final tensor axis)
    elementwise_scale_weight : torch.Tensor
        learned scaling term after normalization?
    elementwise_shift_bias : torch.Tensor
        learned bias term after normalization?
    eps : float
        Safety term to prevent division by zero. Make sure the chosen value fits in the range of your encoding scheme (i.e. fp16 requires eps >= 6e-8).
    elementwise_scale_and_shift : bool
        Include a learned scaling and shift term after normalization.
    """

    def __init__(
        self,
        prefix,
        config,
        weights,
        eps=1e-06,
        elementwise_scale_and_shift=True
    ):
        super(MLPSpeculatorLayerNorm, self).__init__()
        self.elementwise_scale_and_shift = elementwise_scale_and_shift
        if self.elementwise_scale_and_shift:
            self.weight = weights.get_tensor(f"{prefix}.weight")
            self.bias = weights.get_tensor(f"{prefix}.bias")
        self.eps = eps

    def forward(self, x):
        xf = x
        xf = xf * torch.rsqrt(xf.pow(2).mean(-1, keepdim=True) + self.eps)
        x = xf.type_as(x)
        if self.elementwise_scale_and_shift:
            x = self.weight * x
            x = x + self.bias
        return x


class MLPSpeculatorModel(torch.nn.Module):
    def __init__(self, config, prefix, weights):
        super().__init__()
        self.config = config
        self.n_predict = get_speculate()
        self.hidden_size = config.hidden_size
        self.tie_weights = config.speculator_config.get("tie_weights", False)
        self.scale_input = config.speculator_config.get("scale_input", False)
        if self.tie_weights:
            embedding = TensorParallelEmbedding(f"{prefix}.emb.0", weights)
            self.emb = nn.ModuleList([embedding] * self.n_predict)

            proj_first = FastLinear.load(
                config,
                prefix=f"{prefix}.proj.0",
                weights=weights,
                bias=False,
            )
            proj_tied = FastLinear.load(
                config,
                prefix=f"{prefix}.proj.1",
                weights=weights,
                bias=False,
            )
            self.proj = nn.ModuleList([proj_first] + [proj_tied] *
                                      (self.n_predict - 1))
            head = FastLinear.load(config, f"{prefix}.head.0", weights, bias=False)
            self.head = nn.ModuleList([head] * self.n_predict)

            ln = MLPSpeculatorLayerNorm(
                prefix=f"{prefix}.ln.0",
                config=config,
                weights=weights,
                elementwise_scale_and_shift=True
            )
            self.ln = nn.ModuleList([ln] * self.n_predict)
        else:
            self.emb = nn.ModuleList(
                [
                    TensorParallelEmbedding(f"{prefix}.emb.{i}", weights)
                    for i in range(self.n_predict)
                ]
            )
            self.proj = [
                FastLinear.load(
                    config,
                    prefix=f"{prefix}.proj.{i}",
                    weights=weights,
                    bias=False,
                )
                for i in range(self.n_predict)
            ]
            self.head = nn.ModuleList(
                [
                    FastLinear.load(config, f"{prefix}.head.{i}", weights, bias=False)
                    for i in range(self.n_predict)
                ]
            )
            self.ln = nn.ModuleList(
                [
                    MLPSpeculatorLayerNorm(
                        prefix=f"{prefix}.ln.{i}",
                        config=config,
                        weights=weights,
                        elementwise_scale_and_shift=True
                    )
                    for i in range(self.n_predict)
                ]
            )

        if self.scale_input:
            self.ln0 = MLPSpeculatorLayerNorm(self.hidden_size, elementwise_scale_and_shift=False)

        # Weights ensure that state_0 accounts for 50% of state magnitude by final head in expectation
        self.state_weight = 0.5 ** (0.5 / self.n_predict)
        self.emb_weight = math.sqrt(1 - self.state_weight**2)
        self.activation = nn.GELU()
        # TODO
        self.vsize = config.vocab_size
        self.inner_dim = config.speculator_config["inner_dim"]
        self.top_k_tokens_per_head = [1] * self.n_predict

    def forward(
        self,
        hidden_states: torch.Tensor,
        input_ids: torch.Tensor,
    ):
        top_k_tokens_per_head = self.top_k_tokens_per_head

        # k indicates # of candidates
        # h indicates # of generated tokens
        state = hidden_states

        if self.scale_input:
            state = self.ln0(state) / SQRT2

        b = state.size(0)
        ind = input_ids.unsqueeze(0)
        all_probs = torch.empty(
            b, self.n_predict, self.vsize, device=state.device
        )  # b k h v
        assert (
            len(top_k_tokens_per_head) == self.n_predict
        ), f"You must provide a topk number for each head ({self.n_predict} heads, {len(top_k_tokens_per_head)} provided)"
        for i in range(self.n_predict):
            # Project and predict
            z = self.emb[i](ind)
            z = z.mul(self.emb_weight * math.sqrt(self.inner_dim / 2))  # b k d
            state = self.proj[i](state) * self.state_weight + z
            state = self.activation(self.ln[i](state))  # b k d
            probs = F.log_softmax(self.head[i](state), dim=-1)  # b k v
            _probs, preds = probs.topk(top_k_tokens_per_head[i], dim=-1)  # b k k'

            # Update candidate set with new predictions

            # Update distribution set with new logits
            all_probs[:, i] = probs.exp()

            # Update state, log_probs and ind for new predictions
            state = state.unsqueeze(2).expand(
                -1, -1, top_k_tokens_per_head[i], -1
            )  # b k k' d
            state = state.reshape(-1, b, state.size(3))  # b kk' d
            ind = preds.view(-1, b)  # b kk'

        speculative_logits = all_probs
        return speculative_logits


class MLPSpeculatorHead(nn.Module):
    def __init__(self, lm_head, mlp_speculator):
        super().__init__()
        self.lm_head = lm_head
        self.mlp_speculator = mlp_speculator

    def forward(
        self, input: torch.Tensor
    ) -> Tuple[torch.Tensor, Optional[torch.Tensor]]:
        logits = self.lm_head(input)
        # If we have too many tokens, we skip speculative logits
        if input.shape[0] > 128:
            return logits, None

        input_ids = logits.argmax(dim=-1)
        speculative_logits = self.mlp_speculator(input, input_ids)
        return logits, speculative_logits

    @staticmethod
    def load(config, prefix: str, weights):
        from pathlib import Path
        from safetensors import safe_open

        speculator_path = config.speculator["path"]

        for fname in config.speculator["model_paths"]:
            filename = str(Path(speculator_path) / fname)
            routing = weights.routing
            with safe_open(filename, framework="pytorch") as f:
                for k in f.keys():
                    if k in routing and routing[k] != filename:
                        raise RuntimeError(
                            f"Key {k} was found in multiple files: {filename} and {routing[k]}"
                        )
                    routing[k] = filename

        mlp_speculator = MLPSpeculatorModel(config, "speculator", weights)
        lm_head = TensorParallelHead.load(config, prefix, weights)
        return MLPSpeculatorHead(lm_head, mlp_speculator)
