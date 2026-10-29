# Qwen3.6-35B-A3B 主脑后训练：SFT、DPO、评测

要训 35B-A3B 主脑时先读这份。内容来自 2026-10-26 至 27 的讨论，以及在 H100.2 上实测的模型结构和模板哈希。**训练本身还没跑过**，命令参数以当时的框架版本为准。

数据怎么造、配比、风格宪法不在这里，见 gpu2 上的 `/Data/cmbian/codes/mainbrain_27B_model/docs/sft_data_plan.md` 和 `data_pipeline.md`。本文只写换成 35B-A3B 以后怎么训、怎么测。登录见 [../gpu2-h100.2-login.md](../gpu2-h100.2-login.md)。

## 0. 结论

| 问题 | 结论 |
|:--|:--|
| 训什么 | 只训语言部分。视觉塔、MTP 头冻住不管 |
| 方法 | LoRA SFT，过门禁后再做 LoRA DPO。第一期不做全参 |
| 框架 | ms-swift，transformers + peft 后端，不先上 Megatron |
| 基座权重 | 用 BF16，不用盘上的 FP8 |
| router | 冻住，aux loss 设 0 |
| 27B 的数据 | SFT 金标能直接用；通用回放和 DPO 对子要在 35B 上重出 |
| 比 27B 多做的一步 | 训完用评测集做路由体检，和基座对比 |
| 上实车的条件 | 评测集通过，并且路由体检没有标红 |

## 1. 基座

| 项 | 值 |
|:--|:--|
| 训练基座 | `/data/carher/models/Qwen3.6-35B-A3B`，BF16，67G。2026-10-27 从 ModelScope 下载并校验，见 [worklog](worklog/2026-10-27-35b-a3b-bf16-download.md) |
| 盘上现有 | `/data/carher/models/Qwen3.6-35B-A3B-FP8`，只能推理，不能拿来训。不要从 FP8 反量化，丢掉的精度补不回来 |
| 结构 | `qwen3_5_moe`，40 层，hidden 2048 |
| 混合注意力 | 30 层 Gated DeltaNet，10 层普通 attention（第 3、7、…、39 层），每 4 层一次 |
| FFN | 40 层全是 MoE：256 个专家选 8 个，专家中间维 512，每层另有一个 shared expert |
| 专家的存法 | BF16 版里每层的 256 个专家合并成两个 3D 参数：`mlp.experts.gate_up_proj` [256, 1024, 2048] 和 `mlp.experts.down_proj` [256, 2048, 512]，不是 `nn.Linear`。FP8 版是逐专家存的 |
| router | 40 个 `mlp.gate`，每层一个。checkpoint 里有 41 个，多出的在 MTP 头 |
| 不训的部分 | 视觉塔 `model.visual.*`、MTP 头 `mtp.*`、embedding、`lm_head` |
| 训练机 | H100.2，8×H100 80GB |

主脑训练平台 `mainbrain_27B_model/server/train_board.py` 里，`qwen3.6-35b-a3b` 现在指向 FP8 的 hf_cache 快照，开训前要改成 BF16 路径。

## 2. 数据：哪些能直接用

票库里存的是票、原话、前文的结构化描述和金标，不存拼好的 token，所以换基座不用重新造数。

| 数据 | 能不能直接用 | 做法 |
|:--|:--|:--|
| 工具决策类金标：规划、导航、车控选函数、记忆、边界对 | 能 | 金标是该调什么、参数怎么填，跟模型无关 |
| 口播类金标：闲聊、百科、推荐、迎宾 | 能用，最好重起草 | 原来是 27B 起草再最小编辑。最好改成 35B 起草，人只改长度、禁忌和事实，更贴近 35B 自己的分布 |
| 20% 通用回放 | 不能 | 方案要求答案由同一个基座、同样的思考开关生成，只滤掉错的。要在 35B 上重新生成 |
| DPO 对子 | 不能 | 对子是在 27B 的策略上采的，换到 35B 是 off-policy。等 35B 的 SFT 过门禁后在 35B 上重采 |
| 已经 tokenize 好的缓存 | 不能 | 层数、hidden、模板都不同，必须重新渲染 |

### 2.1 模板

H100.2 上实测 `chat_template.jinja` 的 sha256 前 12 位：

| 模型 | 哈希 | 长度 |
|:--|:--|--:|
| Qwen3.6-27B-FP8 | `e84f32a23fdd` | 7764 |
| Qwen3.6-35B-A3B-FP8 | `e84f32a23fdd` | 7764 |
| Qwen3.5-9B | `a4aee8afcf2e` | 7756 |
| Qwen3.8-27B（现网） | `c3cf9e34abf4` | 8952 |

3.6-27B 和 3.6-35B-A3B 是同一份模板。现网是 3.8，它会往 system 里注入 `reasoning_effort`，保留 thinking 的默认值也不一样，所以按 3.8 拼好的样本不能直接拿来训 35B。

### 2.2 导出

沿用 `data_pipeline.md` 的「训练样本怎么拼」：

1. messages 和 tools 由 carHer2 线上代码生成，不能自己拼。
2. 导出时记下 carHer2 的 git 提交号和 runtime 版本。
3. 训练只认票库 tag，不读工作区。
4. 用 35B 自带的 `chat_template.jinja` 渲染，见 §3.2。

## 3. 工具链

### 3.1 框架

| 选择 | 理由 |
|:--|:--|
| ms-swift，transformers + peft 后端 | Qwen 官方文档点名的框架，SFT 和 DPO 都支持 |
| 先不上 Megatron-SWIFT | Megatron 训 MoE 快很多，但有人报过 Qwen3.6-35B-A3B 用 Megatron SFT（全参和 LoRA 都有）后，多轮工具调用停在 `</think>` 或空的 `<tool_call>`。维护者认为是数据和模板的问题，但这正好是我们的场景。先用 peft 跑通，再考虑提速 |
| 不用 LLaMA-Factory | 它的数据集转换器容易改动 carHer2 拼出来的 wire |
| 不用 Unsloth 的 4-bit QLoRA | Unsloth 自己不建议对这个 MoE 做 4-bit。我们有 8×H100，用 BF16 LoRA |
| 并行 | 8 卡 LoRA 加 DeepSpeed ZeRO-3 就够，不需要专家并行 |

H100.2 的 `/data/carher/venv` 现在是 transformers 5.15.0、peft 0.20.0，能加载 `qwen3_5_moe`，但没有装 ms-swift。ms-swift 能不能对 3D 专家参数挂 LoRA 还没验证。不能的话，就用 transformers + peft 自己写训练脚本。用的版本要记进 `job.json`。

### 3.2 模板对齐

ms-swift 默认把 Qwen3.6 绑到 `qwen3_5` 的 agent 模板上。这个模板和官方 jinja 有差别：user 消息尾部多一个换行，历史 assistant 的 `<think>` 两侧换行不同。一条带工具调用的轨迹大约多出 8 到 12 个 token。这种错不报错，模型学的却是另一串。

做法：

1. 用模型自带的 jinja 渲染（`--template_backend jinja`）。
2. 开训前抽 20 条样本，把训练器编码出来的 `input_ids` 和 `tokenizer.apply_chat_template` 的结果逐字节对比，必须完全一致。
3. 不一致就自己写 collator：用 `apply_chat_template` 渲染，再按 assistant 的角色标记算 loss 区间。

### 3.3 loss mask

- 只有最后一轮 assistant 计算 loss，包括它的 tool_call。system、user、历史 assistant、tool 返回都设为 -100。
- 空的 `<think>` 不计算 loss（ms-swift 是 `--loss_scale ignore_empty_think`）。
- 关掉 packing。一条样本的系统提示就有 4000 多 token，packing 容易让 mask 跨样本。
- 抽几条样本打出 label 不为 -100 的 token，人眼确认。

## 4. LoRA 挂在哪

层结构图见 [lora-layers.md](lora-layers.md)。

| 块 | 挂 | 不挂 |
|:--|:--|:--|
| 普通 attention（10 层） | `self_attn.q_proj` `k_proj` `v_proj` `o_proj` | `q_norm` `k_norm` |
| Gated DeltaNet（30 层） | `linear_attn.in_proj_qkv` `in_proj_a` `in_proj_b` `in_proj_z` `out_proj` | `A_log` `dt_bias` `conv1d` `norm` |
| MoE FFN（40 层） | 路由专家的 `mlp.experts.gate_up_proj` `mlp.experts.down_proj`（3D 参数，用 `target_parameters` 挂）；shared expert 的 `gate_proj` `up_proj` `down_proj` | `mlp.gate`（router）、`shared_expert_gate` |
| 其他 | | 视觉塔、MTP 头、embedding、`lm_head`、各层 layernorm |

### 4.1 为什么

LoRA 是在大矩阵旁边加一条低秩旁路：`y = Wx + B(Ax)`。一个模块挂不挂，看三件事：它是不是读写残差流的大矩阵；改它是在改内容，还是在拧全局开关；这次的 loss 碰不碰得到它。

**挂的部分，一层里两种活都要改：**

- 取信息：attention 和 DeltaNet 决定当前位置从前文哪里取东西，比如系统提示里的规则、前文工具返回的地址。
- 加工：FFN 把取到的东西变成下一步动作，比如调哪个工具、话说多短。
- QLoRA 的实验表明，所有线性层都挂才接近全参微调，FFN 尤其重要。
- DeltaNet 占了 40 层里的 30 层。只照老习惯挂 `q_proj` 一类，四分之三的层就训不到。它的 `in_proj_a/b` 决定旧记忆忘多少、新内容写多重，「用户说了新目的地就把旧的忘掉」这类行为就落在这里。

**不挂的部分：**

| 模块 | 理由 |
|:--|:--|
| router `mlp.gate` | 后面接 top-8，是离散选择。微调时会形成正反馈：被选中的专家吃到梯度，router 就更多地选它。几万条车载数据很快就把流量挤到少数专家上，表现为飘、复读、工具不调。要改的是专家怎么做，不是派给谁 |
| `shared_expert_gate` | 每个 token 只输出一个 0 到 1 的数，决定 shared expert 那一路听几成，本身没有内容。每层只有 2048 个参数，rank 16 的 LoRA 补丁比它本身还大十几倍。内容由 shared expert 自己的三层负责，那三层挂了 |
| `A_log` `dt_bias` | 每个头的基础衰减速度，只是一排数，没法挂 LoRA。我们一条样本里有 4000 多 token 的系统提示，衰减的底一变，规则要么在长前缀里被冲掉，要么忘不掉。跟内容有关的部分已经交给 `in_proj_a/b` |
| `conv1d` `norm` | 长度为 4 的短卷积和尺度参数。改了会影响每一个 token 的输入，收益很小 |
| MTP 头 | 这次没有算它的 loss，挂上去梯度也是 0 |
| 视觉塔 | 这次不训视觉 |
| embedding `lm_head` | 没有新增 token，改它们等于改每个词的含义 |

### 4.2 配置和检查

2026-10-27 在 transformers 5.15.0、peft 0.20.0 上用 meta 设备实测过下面这份配置。可训练参数里没有 router、`shared_expert_gate`、视觉塔和 MTP：

```python
from peft import LoraConfig

lang = r"model\.language_model\.layers\.\d+\."
lora = LoraConfig(
    r=16, lora_alpha=32, lora_dropout=0.0,
    target_modules=lang + r"(self_attn\.(q_proj|k_proj|v_proj|o_proj)"
                        r"|linear_attn\.(in_proj_qkv|in_proj_a|in_proj_b|in_proj_z|out_proj)"
                        r"|mlp\.shared_expert\.(gate_proj|up_proj|down_proj))",
    target_parameters=["mlp.experts.gate_up_proj", "mlp.experts.down_proj"],
)
```

- 路由专家是 3D 参数，不是 `nn.Linear`。`target_modules` 和 `all-linear` 都挂不到它们，要用 `target_parameters`。peft 会给每个专家各配一份 rank r 的 LoRA。
- 不要用 `all-linear`：它挂不到专家，却会挂到 `shared_expert_gate`（2048→1 的 Linear）、`lm_head` 和视觉塔。
- router 是 `Qwen3_5MoeTopKRouter`，不是 Linear，按模块名挂不上。但放进 `modules_to_save` 仍会把它解冻。
- `target_modules` 用上面这样的完整正则，不要写 `gate` 或 `.*gate.*`。
- `modules_to_save` 留空，放进去就是整块全参训练。
- MoE 的负载均衡 aux loss 设 0。
- r=16 时可训练参数是 9.44 亿，占 36.05B 的 2.6%，大头在路由专家上，每层约 2300 万。加上 Adam 状态大约 11 GB，8 卡 ZeRO-3 分摊没有压力。显存紧张或者过拟合时，先降低 r。
- 只实测了 `lora_dropout=0.0`。peft 对 `target_parameters` 是否支持 dropout 还没验证，§5.1 的 0.05 要等实测后再定。

开训前检查可训练参数：

```python
names = [n for n, p in model.named_parameters() if p.requires_grad]
assert all("lora_" in n for n in names)
assert not any("visual" in n or "mtp" in n for n in names)
assert not any(n.split(".lora_")[0].endswith(("mlp.gate", "shared_expert_gate")) for n in names)
print(len(names), names[:20])
```

## 5. SFT

### 5.1 起步超参

| 项 | 值 | 说明 |
|:--|:--|:--|
| rank / alpha | 16 / 32 | 1000 条闭环用。3 万条全量可以试 64 |
| 学习率 | 1e-4，cosine，warmup 3% | 路由变窄就减半 |
| epoch | 2 | 每个 epoch 都评测，选最好的检查点，不默认选最后一个 |
| 序列长度 | 6144 | 系统提示均值约 4600 token |
| 精度 | BF16，开梯度检查点 | |
| dropout | 0.05 | 只对 `target_modules` 有把握；`target_parameters` 上要实测，见 §4.2 |

以 smoke 结果为准，这些只是起点。

### 5.2 步骤

1. 零样本基线：35B 原样跑一遍评测集，同时记录路由，作为 §8 的对照。
2. smoke：先用 1000 条最小闭环跑通导出、mask、训练、评测、合并的整条链路。
3. 全量：3 万条，其中 2.4 万领域数据、6 千通用回放。
4. 每个 epoch：跑评测集，加 500 条探针集看路由（§8.4）。
5. 选检查点：评测最好、路由没有标红的那一个。

## 6. DPO

### 6.1 什么时候进 DPO

按 carHer2 `cloud_main_27b_post_training.md` §7.1 的门禁：协议可解析率约 95% 以上，主路径行为 85% 到 90% 以上，错误类型从「不会」变成「会但不稳」。没过门禁就继续 SFT。

DPO 用来压两类问题：一是会做但有时选飘，比如该附近搜却调了规划；二是啰嗦。

### 6.2 原理

同一个前缀下，让 chosen 整段的概率比 rejected 高，并且高出的幅度要超过参考模型：

```text
优势 = [log π(chosen) − log π_ref(chosen)] − [log π(rejected) − log π_ref(rejected)]
loss = −log σ(β × 优势)
```

`log π` 是回复里每个 token 的 logprob 之和，prompt 部分不算。参考模型 π_ref 是冻住的 SFT 检查点。β 控制能离参考模型多远。

### 6.3 采对子

1. 前缀固定，和线上一致，包括 system、tools、历史里的工具调用，只换最后一轮 assistant。
2. 用 35B SFT 检查点采样，每个前缀 4 到 8 条，温度 0.7 到 1.0，`top_p` 0.9。温度太低采出来几乎一样，太高会出现线上不会有的坏 JSON。
3. 裁判只标高低，不改写。标准是工具对不对、参数对不对、有没有越权、风格条款过不过。裁判可以是人，也可以是按规则判的另一个模型，不能是生成模型自己。
4. 只把高低分得开的配成一对：最好对最差，或最好对每一条明显更差的。分不出来的不配对，不要两两全配。
5. chosen 和 rejected 都必须是这个模型自己采出来的。不要用强模型或人重写的版本当 chosen，否则等于在蒸馏别人的文风。
6. chosen 必须已经足够短。DPO 本身偏向长回答，不能指望靠 loss 压短。
7. 对子按 17 项能力的占比分配，不要集中在闲聊。
8. 训完如果还要再压一轮，用新检查点重采，旧对子作废。

### 6.4 例子

| 前缀 | chosen | rejected | 学什么 |
|:--|:--|:--|:--|
| 把空调开了 | 好，空调开了。 | 好的，已经为您打开空调了，请问还有什么需要？ | 短 |
| 附近有没有火锅 | 调附近搜：最近的是望京店，784 米。 | 调规划：先去火锅店，再找停车场 | 工具选对 |
| 导航去公司 | 好，去公司。 | 好的，已为您规划去公司的路线，路上注意安全。 | 两条都是 SFT 模型采出来的，才能配成一对 |

反例：用基座瞎编的回复当 rejected、人重写的回复当 chosen，这不是有效的 DPO 对子。

### 6.5 训练

| 项 | 值 |
|:--|:--|
| 命令 | `swift rlhf --rlhf_type dpo` |
| 参考模型 | SFT 检查点。用 LoRA 时可以关掉 adapter 拿参考 logprob，不需要另载一份模型 |
| LoRA | 在 SFT 合并后的权重上新开一个 adapter，挂的层和 §4 一样 |
| β | 0.1 起 |
| 学习率 | 比 SFT 小一个量级，5e-6 到 1e-5 |
| epoch | 1 |

DPO 以后单独看平均字数和风格条款通过率，不要只看偏好胜率。路由体检也要再做一次。

## 7. 合并和上线

1. adapter 合并进 BF16 基座。
2. 量化成 FP8，交给现有的 vLLM。不要指望 vLLM 直接加载含 `linear_attn` 的 LoRA。
3. 合并后用 FP8 版本再跑一次评测集，确认量化没有掉分。
4. 合并后基座的 prefix cache 不能复用，同样的前缀算出来的 KV 已经不一样，线上要重新预热。
5. 盘上的 BF16 基座始终保持原样，删掉 adapter 就能回到训前。

## 8. 评测

### 8.1 流程

和 27B 一样，只多一步路由体检：

```text
训完 → 5k 评测集（行为）→ 路由体检（和基座比）→ 都过 → 实车
```

行为评测沿用 `sft_data_plan.md` §9.1：各能力场景集、边界对（一对里错一张就算整对错）、风格条款、人设，以及通用基准相对基座掉分不超过 1 到 2 个点。MoE 要特别注意通用掉分，它常常是路由变窄的信号。

路由正常不代表行为没问题，行为通过也不代表路由没问题。两项都要过。

### 8.2 为什么要看路由

router 冻住了，但它的输入是前面各层写出来的残差，这些层都挂了 LoRA。所以同一段 prefill，训后选出来的专家会有一部分不一样。这是预期中的变化：路由映射本身没变，只是表示变了。

但上游有可能学偏，把很多 token 的隐状态推到同一片区域，流量就会挤到少数专家上。这时评测集上的分数可能还不错，问题先出在评测集没覆盖到的说法上，也就是实车上的各种口语。

### 8.3 路由体检

**输入**：5k 评测集，分别过基座（关掉 adapter）和训后模型，用 teacher forcing，逐 token 对齐。

**统计范围**：只统计 assistant 回复的 token。系统提示有 4000 多 token，混进来会把回复上的变化淹没。40 个 router 都统计，MTP 里那个不算。

**每层四个数：**

| 指标 | 怎么算 | 看什么 |
|:--|:--|:--|
| 负载熵 | 每个专家接到的 token 份额算熵，再除以 ln256 | 训后和基座的比值 |
| top-8 重合 | 同一个 token 在两边选的 8 个专家里有几个相同，除以 8 | 只作参考，深层本来就低一些 |
| 冷门专家数 | 份额低于平均值（1/256）十分之一的专家个数 | 比基座多了多少 |
| 最忙专家份额 | 接 token 最多的那个专家占的比例 | 比基座涨了多少 |

**标红规则**（起步值，第一次跑完后按实际情况校准）：

- 熵比值低于 0.90 标红，0.90 到 0.95 标黄
- 冷门专家比基座多 20 个以上，标红
- 最忙专家份额达到基座的 2 倍以上，标红

**一定要和基座比，不要和均匀分布比。** 车载数据在基座上本来就会偏向某些专家。

### 8.4 训练过程中

固定一份约 500 条的探针集，各能力和通用数据混在一起。每次 eval 都跑一遍，把路由曲线和评测分画在同一张图上。

### 8.5 补充检查

需要排查时再做，不是每次都要：

- 用评测 prompt 实际生成一遍，记录生成 token 的路由。teacher forcing 喂的是金标，线上进 router 的是模型自己生成的 token。
- 在通用数据和线上真实对话上也做一次体检。

## 9. Report 格式

脚本自动生成。人只看结论那一行和标红的几行，不用看 40×256 的直方图。

```markdown
# 路由体检：mainbrain-35b-sft-e2 对比 Qwen3.6-35B-A3B 基座

- 评测集：eval_5k（tag step1-b8），统计 assistant token 312,480 个
- 方式：同一批 token 以 teacher forcing 过两个模型，逐层记录 top-8
- 结论：通过。40 层无红，2 层黄（L33、L37）

## 逐层

| 层 | 类型 | 熵 基座 | 熵 训后 | 比值 | top-8 重合 | 冷门专家 基座→训后 | 最忙专家 基座→训后 | 标记 |
|---:|:--|--:|--:|--:|--:|:--|:--|:--|
| 0 | DeltaNet | 0.91 | 0.91 | 1.00 | 0.97 | 3→3 | 1.2%→1.2% | |
| 3 | Attention | 0.88 | 0.87 | 0.99 | 0.93 | 6→6 | 1.5%→1.6% | |
| … | | | | | | | | |
| 33 | DeltaNet | 0.86 | 0.81 | 0.94 | 0.71 | 9→18 | 2.1%→3.0% | 黄 |
| 37 | DeltaNet | 0.85 | 0.80 | 0.94 | 0.69 | 10→21 | 2.2%→3.4% | 黄 |

## 按能力（每项只列最差的一层）

| 能力 | 条数 | 最差层 | 熵比值 | 标记 |
|:--|--:|--:|--:|:--|
| 规划 | 600 | L37 | 0.93 | 黄 |
| 车控 | 500 | L33 | 0.95 | |
| 通用回放 | 1000 | L35 | 0.98 | |
```

上面的数字只是示例。

**怎么读：**

1. 先看结论。没有红就上实车。
2. 有红就看是哪几层。只集中在深层，通常是学习率或 rank 偏大；从浅层就开始掉，多半是数据太窄。
3. 再看按能力那张表，找出最先变窄的能力，回去给那一类补数据或加回放。

## 10. 路由变窄了怎么办

按代价从低到高：

1. **确认是真的。** 和基座比明显变窄，同时评测掉分，才算。先排除假象：用 §3.2 对一遍模板字节，用 §3.3 检查 mask。
2. **配错导致 router 真被训了**（最常见的是把它放进了 `modules_to_save`，或者自己写训练循环时忘了冻结）：把 router 权重换回基座原值，或者删掉 adapter 里 gate 那几组再合并。然后冻住 router，用低学习率补训一小轮。
3. **回到更早的检查点。** 路由变窄通常是逐步发生的。
4. **步子放小。** 学习率减半，rank 从 64 降到 16，减少 epoch。
5. **调数据。** 见 §11。
6. **加路由约束。** 在同一批 token 上，约束训练中的路由分布不要离基座太远。router 冻住时，这个 loss 的梯度只会流回上游 LoRA：

```python
import torch
import torch.nn.functional as F

with torch.no_grad(), model.disable_adapter():
    ref = model(**batch, output_router_logits=True).router_logits
out = model(**batch, output_router_logits=True)

# KL(基座路由 || 当前路由)，每层平均。实际要按 attention_mask 去掉 padding
kl = sum(
    F.kl_div(F.log_softmax(cur.float(), -1), F.softmax(base.float(), -1), reduction="batchmean")
    for cur, base in zip(out.router_logits, ref)
) / len(ref)
loss = out.loss + lam * kl  # lam 从 0.01 开始试
```

   每步多一次不算梯度的前向，算力大约多三四成。`router_logits` 的形状以 transformers 版本为准。更省事的替代是打开负载均衡 aux loss，系数 1e-3。但它把路由拉向均匀分布，不是拉回基座，所以不如 KL 准。

7. **缩小 LoRA 范围。** 只在变窄的那几层去掉或降低 attention/DeltaNet 的 rank，专家 FFN 保留。这是经验做法。
8. **退回。** BF16 基座没动过，删掉 adapter 就回到训前。

## 11. 找出问题数据、调配比

路由变窄几乎都是数据在 token 层面太窄：同一类句式反复出现，把隐状态推到同一片区域。思路是先从路由反查到样本，再按标签归到组，确认因果后再调配比。

### 11.1 从路由反查样本

体检里标红的那几层，就是「热」的层。热层里份额比基座涨得最多的专家，就是「热专家」。

对**训练集**每条样本，只看 assistant token，在基座和训后模型上各算一次，得到两个分数：

| 分数 | 怎么算 | 含义 |
|:--|:--|:--|
| 热专家占比增量 | 这条样本的 token 在热层被派给热专家的比例，训后减基座 | 这条样本把流量往热专家推了多少 |
| 路由 KL | 热层上训后和基座路由分布的 KL，在 token 上取平均 | 这条样本的路由被改了多少 |

按分数从高到低排序，取前 1% 到 5%，人工抽看几十条。

### 11.2 按标签归组

每条样本 `meta` 里都有 `capability`、`subtype`、对抗类型、风格档、来源、写票模型、prompt 版本（`sft_data_plan.md` §8.1）。把高分样本按这些标签分组，和全集的占比比较：

```text
组的富集度 = 该组在高分样本里的占比 / 该组在全集里的占比
```

富集度明显大于 1 的组，就是嫌疑组。常见的嫌疑：

- 某个写票模板或批次，句式高度相似
- 风格档里的确认类短句，句子短、结构单一、量又大
- 某项能力的金标格式过于固定，比如 tool_call 参数都长一个样
- 同一个写票模型批量生成的原话

### 11.3 补一个看文本的视角

标签粒度不够时，用 H100.2 上已经在跑的 `qwen3-embedding-8b`（`127.0.0.1:8019`）给 assistant 文本和用户原话做向量，再聚类：

- 簇很大、簇内相似度又很高，就是模板塌缩。按 SemDeDup 的做法，在簇内去掉近重复的样本。
- 把簇和 11.1 的高分样本交叉，看热专家是不是集中在少数几个簇里。

### 11.4 确认因果

相关不等于因果。动全量之前，先在 1000 条闭环的规模上做一次留组对照：去掉嫌疑组，或者把它降到一半，重训后复测热层。热层恢复，就确认是这组数据的问题。

更精细的办法是梯度归因（TracIn、LESS）：直接算出每条训练样本对某个评测指标的影响。准是准，但算力开销大，一般在上面的办法定位不到时才用。

### 11.5 调配比

确认嫌疑组以后，按代价从低到高：

1. **去重、设上限。** 近重复样本去掉，同一模板设条数上限。这是最常见、最有效的一步。
2. **嫌疑组降采样，同时补多样性。** 不是简单删掉，而是同一意图换说法重写：换句式、换长度、换前文。总条数不变，但每个档的下限（`sft_data_plan.md` §5）要守住。
3. **能力之间用温度采样。** 按 `p ∝ n^(1/T)` 采样，T 取 2 到 3。大组被压下去，小组被抬上来，不会出现一类数据压倒其余类的情况。
4. **加回放。** 通用回放从 20% 提到 30%，仍然用 35B 自己生成的答案。
5. **小规模试配比。** 在 1000 条的规模上试三到五组配比，用「评测分加路由体检」一起选，再放到全量上。DoReMi 那种用代理模型自动学配比的方法是给预训练用的，后训练这个规模用小网格搜索就够了。

调完以后重新训练、重新做体检。每次调整都存一个新的票库 tag，报告里写上用的是哪个 tag，方便以后回查。

### 11.6 业界做法对照

| 问题 | 做法 | 出处 |
|:--|:--|:--|
| 近重复和模板塌缩 | 用向量聚类后在簇内去重 | SemDeDup（Abbas et al., 2023） |
| 哪些样本影响了某个指标 | 梯度归因 | TracIn（Pruthi et al., 2020）、LESS（Xia et al., 2024） |
| 多个领域怎么配比 | 代理模型学权重；用配比规律预测 | DoReMi（Xie et al., 2023）、Data Mixing Laws（Ye et al., 2024） |
| 大小组不平衡 | 按温度采样 | 多语言预训练里的常见做法 |
| MoE 微调只动相关专家 | 按任务选专家训，其余冻住 | ESFT（DeepSeek, 2024） |

§11.1 的「热专家占比」和 §11.2 的富集度，是根据我们这次的场景定的诊断办法，不是业界现成的标准，第一次用时要结合抽样人工看。

## 12. 还没定的

- 训练器：H100.2 上没装 ms-swift。要先确认它能对 3D 专家参数挂 LoRA，不能就用 transformers + peft 自己写。
- 模板逐字节对齐还没做。transformers 5.15.0 已经能识别 `qwen3_5_moe`。
- 路由体检脚本和 report 生成脚本还没写。
- 路由标红阈值要在第一次实跑后校准。
- 训练平台 `train_board.py` 里 35B 的模型路径要改成 BF16。

## 参考

- [Qwen 文档：ms-swift](https://qwen.readthedocs.io/en/latest/training/ms_swift.html)
- [Qwen3 MoE Megatron 训练说明](https://github.com/QwenLM/Qwen3/discussions/1301)
- [ms-swift：qwen3_5 模板和 Qwen3.6 jinja 不一致](https://github.com/modelscope/ms-swift/issues/9276)
- [ms-swift：MoE 微调后工具调用被截断](https://github.com/modelscope/ms-swift/issues/9234)
- [ms-swift：冻结 router 的选项](https://github.com/modelscope/ms-swift/issues/5512)
- [为什么不能在 5090 上训 35B MoE（Unsloth 实践）](https://note.com/zephel01/n/n6e1b7e127418?hl=en)
