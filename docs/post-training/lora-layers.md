# Qwen3.6-35B-A3B：LoRA 挂在哪

绿色是挂 LoRA 的，灰色是冻住的。40 层语言层里，30 层是 Gated DeltaNet，10 层是普通 attention，每 4 层一次。每层后面都是同一块 MoE FFN：256 个专家，每个 token 选 8 个，外加一个始终在走的 shared expert。

只训语言部分。视觉塔、MTP 头、embedding、`lm_head` 都冻住，图里不画。原理和完整训练方案见 [qwen36-35b-a3b.md](qwen36-35b-a3b.md) §4。

```mermaid
flowchart TB
  IN[残差流] --> KIND{这一层}

  KIND -->|30 层| DN
  KIND -->|10 层，每 4 层一次| ATT

  subgraph DN [Gated DeltaNet]
    direction TB
    dq[in_proj_qkv]:::lora
    dab[in_proj_a / in_proj_b]:::lora
    dz[in_proj_z]:::lora
    dout[out_proj]:::lora
    dfreeze[A_log / dt_bias / conv1d / norm]:::freeze
  end

  subgraph ATT [普通 Attention]
    direction TB
    aq[q_proj / k_proj / v_proj / o_proj]:::lora
    an[q_norm / k_norm]:::freeze
  end

  DN --> FFN
  ATT --> FFN

  subgraph FFN [MoE FFN，每层都有]
    direction TB
    router[mlp.gate 路由]:::freeze
    experts[选中的 8 个专家<br/>experts.gate_up_proj / experts.down_proj]:::lora
    shared[shared expert<br/>gate_proj / up_proj / down_proj]:::lora
    sgate[shared_expert_gate]:::freeze
    router --> experts
    experts --> mix[汇合写回残差]
    shared --> mix
    sgate --> mix
  end

  mix --> OUT[下一层残差流]

  classDef lora fill:#d9f2d9,stroke:#2e7d32,color:#1b5e20
  classDef freeze fill:#eeeeee,stroke:#9e9e9e,color:#616161
```

BF16 版里路由专家是两个 3D 参数，要用 peft 的 `target_parameters` 挂，配置见 [qwen36-35b-a3b.md](qwen36-35b-a3b.md) §4.2。不要用 `all-linear`：它挂不到专家，却会挂到 `shared_expert_gate`、`lm_head` 和视觉塔。
