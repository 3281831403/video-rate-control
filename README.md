# video-rate-control

## 题面

上线自适应码率后，缓冲区越跑越满、场景切帧还把总预算撑爆：qp_for_bits 反解时漏掉 c 常数项，QP 整体偏移，总码率偏差 8%；vbv_level 只加不排空，漏掉 max_rate/fps 的 drain 项，缓冲区单调上涨必然上溢；frame_bits 用线性代替指数，QP+6 时比特数只减半而不是 1/4，码率大幅超标；plan_gop 按帧均分预算而不是按复杂度加权，复杂度高的帧 QP 过小、总预算超支；scene_bonus 加权后不重新归一，总预算被放大 ratio 倍；smooth_qp 的 max_step 按“QP 差值”而不是“每帧变化”，GOP 边界阶跃仍存在；qp_for_bits 向上取整而不是四舍五入，长期累计 QP 偏高，码率偏低；vbv_level 用秒而不是帧更新，帧率变化时水位错误；validate_plan 只检查上溢不检查下溢，缓冲区空转（underflow）未被发现；control_batch 在场景切换处重置预算但不重置缓冲区，跨片状态泄漏。核心是三条守恒：比特预算、缓冲区水位、QP 与码率的单调关系。ratecontrol/vbv.py 是核心，tests/test_vbv.py 15个红。要修的是：反解补回 c 项并四舍五入取整，VBV 每帧排空 max_rate*1000/fps，恢复 QP 每 +6 比特数降到 1/4 的指数模型，GOP 预算按复杂度加权，场景权重归一化到帧数，QP 逐帧限幅并跨越 GOP 边界，校验上下溢、总预算误差与 QP 阶跃，批量控制时每片独立重置缓冲区并隔离状态。

## 目录

```
video-rate-control/
  README.md
  conftest.py
  .gitignore
  ratecontrol/__init__.py
  ratecontrol/vbv.py
  tests/test_vbv.py
```

## 运行方式

在仓库根目录执行：

```bash
python -m pytest tests -q
```

初始状态预期为恰好 `15 failed`，且没有 collection error、import error 或 syntax error；15 条失败全部来自 `ratecontrol/vbv.py` 中的 `NotImplementedError` 桩。补全实现后目标为 `15 passed`。

环境为 Python 3 + pytest，全程离线、无随机数据、无 sleep、无外部依赖。

## 字段表

| 字段 | 取值 |
| --- | --- |
| 所属领域 | 游戏、图形与多媒体 |
| 子方向 | 音视频处理 |
| 任务类型 | Bugfix |
| 语言/框架 | Python 3, pytest |
| 环境可复现等级 | 无外部依赖 |

## 契约速查

- 码率单位 kbps，帧率单位 fps，帧比特数、VBV 水位与 GOP 预算单位 bit；一帧时长为 `1000 / fps` 毫秒。
- 帧比特模型：`bits = complexity * k * 2 ** (-qp / 3) + c`；QP 每 +6，指数项降为 1/4。
- QP 为 0..51 的整数；`qp_for_bits` 求连续解后四舍五入（.5 向上）并钳制。
- VBV 每帧水位：`buffer + bits - max_rate * 1000 / fps`，不在此函数内钳制；小于 0 为下溢，超过容量由上溢检查报告。
- `plan_gop` 按复杂度加权分配预算：`target_i = budget_bits * complexity_i / sum(complexities)`，再逐帧反解 QP。
- `scene_bonus` 把切换帧原始权重 `ratio` 与非切换帧权重 1.0 同比例归一，使权重和等于帧数；乘到等分的逐帧预算上不会改变总预算。
- `smooth_qp` 逐帧限幅，相邻 QP 变化不得超过 `max_step`，GOP 边界同样生效。
- 批量控制中每片都从 `initial_buffer` 重新开始，片与片之间不得共享任何可变状态。
- 测试对码率断言使用 ±1 kbps 容差（1 秒 GOP 上等价于 ±1000 bit），对总预算和场景权重使用 1 bit 容差。

## tests 不可修改

`tests/test_vbv.py` 是评测基准：不得修改、新增、删除或跳过其中任何测试；修复只能改 `ratecontrol/vbv.py`（README 可补充说明）。不得通过伪造返回值、特判测试输入或修改测试来让测试变绿。
