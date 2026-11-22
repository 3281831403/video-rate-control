"""基于虚拟缓冲区（VBV）的帧级码率控制。

单位与约定
----------
* 码率统一使用 kbps（kilobits per second），帧率使用 fps。
* 帧比特数、VBV 水位与 GOP 预算统一使用 bit；一帧的时长为
  ``1000 / fps`` 毫秒（ms），因此每帧的 drain 为
  ``max_rate * (1000 / fps)`` bit。
* QP 是 0..51 的整数；QP 越大量化越粗，同样的复杂度下帧比特数越少。
* 帧比特模型为 ``complexity * k * 2 ** (-qp / 3) + c``：
  QP 每增加 6，指数项降为原来的 1/4。
* 测试对码率断言使用 ±1 kbps 容差（1 秒 GOP 上等价于 ±1000 bit），
  对总预算与场景权重使用 1 bit 容差。
* 所有函数都是纯函数，不在模块级保存跨调用、跨片的状态。
"""

import math

# QP 的合法范围。
QP_MIN = 0
QP_MAX = 51

# 码率误差容差：1 kbps。
RATE_TOLERANCE_BITS = 1000.0


def _clamp(value, low, high):
    """把 value 钳制到 [low, high]。"""
    return max(low, min(high, value))


def frame_bits(complexity, qp, k, c):
    """按指数模型计算一帧编码后的比特数。

    模型:
        ``bits = complexity * k * 2 ** (-qp / 3) + c``

    参数:
        complexity: 帧复杂度，正数，无量纲。
        qp: 量化参数，0..51 的整数。
        k: 码率系数，正数，单位为 bit；``complexity * k`` 是 QP=0
            时指数项的比特数。
        c: 每帧固定开销，非负数，单位 bit（头信息、填充等）。

    返回:
        ``float``，该帧的比特数，单位 bit。

    契约:
        QP 与比特数是指数关系：QP 增加 6，指数项降为 1/4，即
        ``frame_bits(C, qp + 6, k, c) - c`` 等于
        ``(frame_bits(C, qp, k, c) - c) / 4``。
        complexity <= 0、k <= 0、c < 0 或 qp 不在 0..51 时抛
        ``ValueError``。c=0 时 QP+6 恰好对应比特数除以 4。
    """
    if complexity <= 0:
        raise ValueError("complexity 必须为正数")
    if k <= 0:
        raise ValueError("k 必须为正数")
    if c < 0:
        raise ValueError("c 不能为负数")
    if not QP_MIN <= qp <= QP_MAX:
        raise ValueError("qp 必须在 0..51 范围内")
    return complexity * k * 2.0 ** (-qp / 3.0) + c


def vbv_level(buffer, bits, fps, max_rate):
    """编码一帧后更新 VBV 缓冲区水位。

    参数:
        buffer: 编码该帧前的缓冲区水位，单位 bit；可以为负（已 underflow）。
        bits: 该帧实际产生的比特数，单位 bit，非负。
        fps: 帧率，单位帧/秒，必须大于 0。
        max_rate: 码率上限，单位 kbps，非负。

    返回:
        ``buffer + bits - max_rate * 1000.0 / fps``，单位 bit。

    契约:
        每调用一次只推进一帧，必须排空一帧的 drain
        ``max_rate * (1000 / fps)`` bit，不能只累加 bits；
        也不能按整秒 drain。时间基换算以帧为单位：fps=30 时每帧
        drain ``max_rate * 1000 / 30``，fps=60 时每帧 drain
        ``max_rate * 1000 / 60``，因此相同 max_rate 下 60 fps 每帧
        drain 得更少。
        本函数不做 0..size 的钳制：返回值小于 0 表示 underflow，
        大于缓冲区容量由上层的 :func:`validate_plan` 判定。
        帧率为 0、bits < 0 或 max_rate < 0 时抛 ``ValueError``。
    """
    if fps <= 0:
        raise ValueError("fps 必须大于 0")
    if bits < 0:
        raise ValueError("bits 不能为负数")
    if max_rate < 0:
        raise ValueError("max_rate 不能为负数")
    return buffer + bits - max_rate * 1000.0 / fps


def qp_for_bits(target_bits, complexity, k, c):
    """由目标帧比特数反解 QP。

    反解公式:
        ``qp = -3 * log2((target_bits - c) / (complexity * k))``

    参数:
        target_bits: 目标帧比特数，单位 bit。
        complexity: 帧复杂度，正数。
        k: 码率系数，正数，单位 bit。
        c: 每帧固定开销，非负数，单位 bit。

    返回:
        0..51 的整数 QP。连续解按“四舍五入”取整（小数部分恰为
        0.5 时向上取整），再钳制到 0..51。

    契约:
        反解必须保留 ``c`` 常数项；漏掉它会让同一目标比特数解出
        偏小的 QP，实际码率整体偏高。
        ``target_bits <= c``（目标低于固定开销，指数项无解）时返回
        51；目标高于 QP=0 时的比特数时返回 0。
        结果是四舍五入而不是向上取整，也不是截断。
        complexity <= 0、k <= 0、c < 0 时抛 ``ValueError``。
    """
    if complexity <= 0:
        raise ValueError("complexity 必须为正数")
    if k <= 0:
        raise ValueError("k 必须为正数")
    if c < 0:
        raise ValueError("c 不能为负数")
    # 目标低于固定开销，指数项无解，使用最粗的量化。
    if target_bits <= c:
        return QP_MAX
    # 保留 c 常数项的反解：先扣除固定开销再取对数。
    qp = -3.0 * math.log2((target_bits - c) / (complexity * k))
    # 四舍五入，小数部分恰为 0.5 时向上取整（不能用内置 round，
    # 它是银行家舍入），再钳制到合法 QP 范围。
    qp = int(math.floor(qp + 0.5))
    return _clamp(qp, QP_MIN, QP_MAX)


def plan_gop(frames, budget_bits, config):
    """为一个 GOP 逐帧分配 QP，使总比特贴近预算。

    参数:
        frames: 按显示顺序排列的帧复杂度列表，元素为正数；
            空列表返回 ``[]``。
        budget_bits: 该 GOP 的总比特预算，单位 bit，必须大于 0。
            GOP 时长为 ``len(frames) * 1000 / fps`` 毫秒。
        config: 至少包含 ``k``、``c``；可选 ``qp_min``（默认 0）、
            ``qp_max``（默认 51）、``fps``。``fps`` 仅用于把总比特
            折算成 kbps 检查容差，不改变逐帧分配。

    分配规则:
        预算按复杂度加权，而不是按帧数均分：
        ``target_i = budget_bits * complexity_i / sum(complexities)``。
        然后 ``qp_i = qp_for_bits(target_i, complexity_i, k, c)``，
        再钳制到 ``[qp_min, qp_max]``。

    返回:
        ``list[int]``，长度与 ``frames`` 相同，每个元素是 0..51 的 QP。

    契约:
        复杂度高的帧必须分到更多比特：在未触发钳制且 c=0 时，
        每帧比特数之比等于复杂度之比，逐帧 QP 相同。
        总比特与 ``budget_bits`` 的误差不超过 1 kbps（1000 bit）；
        当预算恰好落在整数 QP 上时误差应为 0（允许浮点误差）。
        不允许不按复杂度加权而直接使用 ``budget_bits / len(frames)``
        作为每帧目标，否则高复杂度帧的码率会被压错。
        budget_bits <= 0 或 qp_min > qp_max 时抛 ``ValueError``。
    """
    if budget_bits <= 0:
        raise ValueError("budget_bits 必须大于 0")
    qp_min = config.get("qp_min", QP_MIN)
    qp_max = config.get("qp_max", QP_MAX)
    if qp_min > qp_max:
        raise ValueError("qp_min 不能大于 qp_max")
    if not frames:
        return []
    k = config["k"]
    c = config["c"]
    total_complexity = sum(frames)
    qps = []
    for complexity in frames:
        if complexity <= 0:
            raise ValueError("帧复杂度必须为正数")
        # 按复杂度加权分配预算，而不是按帧数均分。
        target_bits = budget_bits * complexity / total_complexity
        qp = qp_for_bits(target_bits, complexity, k, c)
        qps.append(_clamp(qp, qp_min, qp_max))
    return qps


def scene_bonus(complexities, ratio):
    """为场景切换帧生成归一化的预算权重。

    参数:
        complexities: 按显示顺序排列的帧复杂度列表。
        ratio: 场景切换判定系数，必须 >= 1。

    返回:
        与输入等长的 ``list[float]`` 权重；输入为空时返回 ``[]``。

    判定与归一化:
        第 0 帧不是切换帧；对 i >= 1，若
        ``complexities[i] >= ratio * complexities[i - 1]``，则第 i 帧
        是场景切换帧。切换帧的原始权重为 ``ratio``，其余帧为 1.0。
        最后把所有原始权重同比例缩放，使 ``sum(weights) ==
        len(complexities)``；缩放后平均权重恒为 1，所以把权重乘到
        等分的逐帧预算上不会改变总预算。

    契约:
        必须重新归一化；未归一化时总权重约为帧数的 ratio 倍，会把
        GOP 总预算放大。权重之和与帧数之差不超过 1e-9。
        ratio < 1 时抛 ``ValueError``。
    """
    if ratio < 1:
        raise ValueError("ratio 必须 >= 1")
    if not complexities:
        return []
    raw_weights = [1.0]
    for i in range(1, len(complexities)):
        if complexities[i] >= ratio * complexities[i - 1]:
            raw_weights.append(float(ratio))
        else:
            raw_weights.append(1.0)
    # 重新归一化到帧数：平均权重恒为 1，总预算不被放大。
    scale = len(complexities) / sum(raw_weights)
    return [weight * scale for weight in raw_weights]


def smooth_qp(qps, max_step):
    """限制相邻帧 QP 的变化幅度。

    参数:
        qps: 按显示顺序排列的 QP 列表（元素为整数或可比较的数值）。
        max_step: 相邻两帧 QP 差值的绝对值上限，非负整数。

    返回:
        ``list[int]``，与输入等长；空输入返回 ``[]``。

    规则:
        首帧保持为 ``clamp(qps[0], 0, 51)``。
        之后逐帧向前钳制：
        ``out[i] = clamp(qps[i], out[i-1] - max_step,
        out[i-1] + max_step)``，并把结果限制在 0..51。

    契约:
        输出中任意相邻两帧满足 ``abs(out[i] - out[i-1]) <= max_step``，
        GOP 边界（例如第 0..4 帧为一段、第 5..9 帧为下一段）也必须
        满足；不能只在段内平滑而让边界出现阶跃。
        ``max_step`` 按“每帧变化”解释，而不是按整段首尾 QP 差值。
        max_step < 0 时抛 ``ValueError``。
    """
    if max_step < 0:
        raise ValueError("max_step 不能为负数")
    if not qps:
        return []
    out = [_clamp(qps[0], QP_MIN, QP_MAX)]
    for qp in qps[1:]:
        # 逐帧限幅：相对上一帧（含跨 GOP 边界）的每帧变化受限。
        previous = out[-1]
        limited = _clamp(qp, previous - max_step, previous + max_step)
        out.append(_clamp(limited, QP_MIN, QP_MAX))
    return [int(qp) for qp in out]


def validate_plan(qps, buffer_levels, budget):
    """检查一份码率控制计划，返回问题描述列表。

    参数:
        qps: 逐帧 QP 列表。
        buffer_levels: 逐帧编码后的 VBV 水位列表，单位 bit。
        budget: 描述预算与容量的字典，至少包含
            ``buffer_size``（bit）、``budget_bits``（bit）、
            ``actual_bits``（bit）；可选 ``max_qp_step``（默认 51）。

    检查项（可同时命中多项）:
        * 水位下溢：任一水位小于 0，问题串包含 ``"underflow"``；
        * 水位上溢：任一水位大于 ``buffer_size``，问题串包含
          ``"overflow"``；
        * 总比特：``abs(actual_bits - budget_bits) >`` 1 kbps
          （1000 bit）时报告，问题串包含 ``"budget"``；
        * QP 段：相邻 QP 变化超过 ``max_qp_step`` 时报告，问题串
          包含 ``"qp"``。

    返回:
        ``list[str]``，每个元素描述一个问题；完全合法时返回 ``[]``。
        该函数只报告问题，不抛异常；同一份计划可以同时报告上下溢。

    契约:
        上溢与下溢都必须检查，不能只检查上溢；缓冲区空转
        （underflow）必须被发现。水位边界 0 与 ``buffer_size``
        本身合法。
    """
    issues = []
    buffer_size = budget["buffer_size"]
    # 下溢与上溢都检查；边界 0 与 buffer_size 本身合法。
    if any(level < 0 for level in buffer_levels):
        issues.append("VBV underflow: 缓冲区水位小于 0")
    if any(level > buffer_size for level in buffer_levels):
        issues.append("VBV overflow: 缓冲区水位超过 buffer_size")
    if abs(budget["actual_bits"] - budget["budget_bits"]) > RATE_TOLERANCE_BITS:
        issues.append("budget overrun: 总比特超出预算 1 kbps 容差")
    max_qp_step = budget.get("max_qp_step", QP_MAX)
    if any(abs(curr - prev) > max_qp_step for prev, curr in zip(qps, qps[1:])):
        issues.append("qp step: 相邻帧 QP 变化超过 max_qp_step")
    return issues


def control_batch(clips, config):
    """对多段视频批量执行码率控制，各片状态完全独立。

    参数:
        clips: 列表，元素为
            ``{"name": str, "frames": [complexity, ...],
            "budget_bits": number}``；``name`` 不得重复。
        config: 包含 ``k``、``c``、``fps``、``buffer_size``、
            ``initial_buffer``、``max_rate_kbps``、``scene_ratio``、
            ``max_step``，以及可选的 ``qp_min`` / ``qp_max``。

    处理流程（每片都从 ``initial_buffer`` 重新开始）:
        1. ``weights = scene_bonus(frames, scene_ratio)``；
        2. 用 ``complexity_i * weight_i`` 调用 :func:`plan_gop` 得到
           逐帧 QP；
        3. 用 :func:`smooth_qp` 限制相邻 QP 变化；
        4. 用原始 ``complexity_i`` 和 :func:`frame_bits` 计算实际
           比特数，再用 :func:`vbv_level` 逐帧推进水位；
        5. 用 :func:`validate_plan` 生成问题列表。

    返回:
        ``{name: {"qps": [...], "levels": [...],
        "total_bits": float, "final_level": float,
        "issues": [...]}}``，键的插入顺序与 ``clips`` 一致；每个片、
        每个列表都是独立对象。

    契约:
        片与片之间不得共享预算游标、VBV 水位或任何可变状态；上一片
        结束时的水位不能成为下一片的起始水位。两片批量控制的结果
        必须与各自单独控制的结果完全相同（浮点值逐项相等）。
        任一 ``name`` 重复时抛 ``ValueError``。
    """
    seen_names = set()
    for clip in clips:
        name = clip["name"]
        if name in seen_names:
            raise ValueError(f"clip 名称重复: {name}")
        seen_names.add(name)

    k = config["k"]
    c = config["c"]
    fps = config["fps"]
    buffer_size = config["buffer_size"]
    initial_buffer = config["initial_buffer"]
    max_rate_kbps = config["max_rate_kbps"]
    scene_ratio = config["scene_ratio"]
    max_step = config["max_step"]

    results = {}
    for clip in clips:
        name = clip["name"]
        frames = list(clip["frames"])
        budget_bits = clip["budget_bits"]

        # 1. 场景切换权重（内部已归一化到帧数，不放大总预算）。
        weights = scene_bonus(frames, scene_ratio)
        # 2. 加权复杂度送入 GOP 规划，按复杂度分配预算。
        weighted_frames = [
            complexity * weight for complexity, weight in zip(frames, weights)
        ]
        planned = plan_gop(weighted_frames, budget_bits, config)
        # 3. 逐帧 QP 限幅，跨 GOP 边界同样生效。
        qps = smooth_qp(planned, max_step)

        # 4. 每片独立的 VBV 状态：从 initial_buffer 重新开始，
        #    实际比特用原始复杂度计算。
        level = initial_buffer
        levels = []
        total_bits = 0.0
        for complexity, qp in zip(frames, qps):
            bits = frame_bits(complexity, qp, k, c)
            total_bits += bits
            level = vbv_level(level, bits, fps, max_rate_kbps)
            levels.append(level)

        # 5. 上下溢、总预算误差与 QP 阶跃一并校验。
        issues = validate_plan(
            qps,
            levels,
            {
                "buffer_size": buffer_size,
                "budget_bits": budget_bits,
                "actual_bits": total_bits,
                "max_qp_step": max_step,
            },
        )

        results[name] = {
            "qps": qps,
            "levels": levels,
            "total_bits": total_bits,
            "final_level": levels[-1] if levels else initial_buffer,
            "issues": issues,
        }
    return results
