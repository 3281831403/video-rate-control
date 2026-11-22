"""VBV 码率控制的验收测试：初始状态下 15 条全部为红。

约定：码率单位 kbps，帧比特数与缓冲区水位单位 bit，QP 为 0..51。
所有测试只依赖标准库与 pytest，不联网、不 sleep、不使用随机数。
"""

import pytest

from ratecontrol.vbv import (
    control_batch,
    frame_bits,
    plan_gop,
    qp_for_bits,
    scene_bonus,
    smooth_qp,
    validate_plan,
    vbv_level,
)

CONFIG = {
    "k": 1.0,
    "c": 0.0,
    "fps": 30.0,
    "buffer_size": 800_000.0,
    "initial_buffer": 500_000.0,
    "max_rate_kbps": 600.0,
    "scene_ratio": 2.0,
    "max_step": 4,
}


def test_qp_for_bits_accounts_for_constant_term():
    """缺陷 1：反解漏掉 c 常数项，QP 整体偏移、总码率偏高。"""
    target_bits = 2000.0
    complexity = 1000.0
    k = 10.0
    c = 500.0

    qp = qp_for_bits(target_bits, complexity, k, c)
    actual = frame_bits(complexity, qp, k, c)

    assert qp == 8
    assert actual == pytest.approx(target_bits, rel=0.05)
    # 漏掉 c 时会解出 7，实际比特数高出约 24%。
    assert frame_bits(complexity, 7, k, c) > target_bits * 1.2


def test_vbv_level_drains_every_frame():
    """缺陷 2：只加不排空，漏掉 drain 项，水位必然上溢。"""
    level = 500_000.0
    bits = 100_000.0
    fps = 30.0
    max_rate = 3000.0  # 每帧 drain = 3000 * 1000 / 30 = 100000 bit

    levels = []
    for _ in range(30):
        level = vbv_level(level, bits, fps, max_rate)
        levels.append(level)

    assert all(item == pytest.approx(500_000.0) for item in levels)
    # 只累加不排空时，30 帧后会涨到 3.5M bit。
    assert max(levels) < 600_000.0


def test_frame_bits_quarters_every_six_qp():
    """缺陷 3：用线性代替指数，QP+6 只减半而不是 1/4。"""
    complexity = 1_000_000.0
    base = frame_bits(complexity, 12, 1.0, 0.0)
    plus_six = frame_bits(complexity, 18, 1.0, 0.0)
    plus_twelve = frame_bits(complexity, 24, 1.0, 0.0)

    assert base == pytest.approx(62_500.0)
    assert plus_six == pytest.approx(base / 4)
    assert plus_twelve == pytest.approx(base / 16)


def test_plan_gop_weights_budget_by_complexity():
    """缺陷 4：按帧均分预算而不是按复杂度加权，高复杂度帧码率被压错。"""
    frames = [5_000_000.0, 1_000_000.0, 1_000_000.0]
    budget_bits = 437_500.0
    config = {"k": 1.0, "c": 0.0, "fps": 30.0}

    qps = plan_gop(frames, budget_bits, config)
    bits = [frame_bits(c, q, 1.0, 0.0) for c, q in zip(frames, qps)]

    assert qps == [12, 12, 12]
    assert bits[0] == pytest.approx(312_500.0)
    assert bits[0] == pytest.approx(5 * bits[1])
    assert sum(bits) == pytest.approx(budget_bits, abs=1000.0)
    # 均分预算时目标为 437500/3，逐帧反解会解出 [15, 8, 8]，
    # 实际总比特涨到 468750，超出预算 7%。
    assert sum(frame_bits(c, 15 if i == 0 else 8, 1.0, 0.0) for i, c in enumerate(frames)) > budget_bits


def test_scene_bonus_renormalizes_weights():
    """缺陷 5：场景加权后不归一，总预算被放大 ratio 倍。"""
    complexities = [1000.0, 1000.0, 3000.0, 3000.0, 1000.0]

    weights = scene_bonus(complexities, 2.0)

    assert len(weights) == 5
    assert sum(weights) == pytest.approx(5.0, abs=1e-9)
    assert weights[2] > weights[0]
    assert weights[2] == pytest.approx(2 * weights[0])


def test_smooth_qp_caps_every_adjacent_step_across_gop():
    """缺陷 6：只在段内平滑，GOP 边界仍出现 QP 阶跃。"""
    qps = [10] * 5 + [30] * 5 + [10] * 5

    out = smooth_qp(qps, 3)

    assert out[0] == 10
    assert all(abs(b - a) <= 3 for a, b in zip(out, out[1:]))
    # 跨过 GOP 边界（下标 4->5）也必须逐帧爬升。
    assert out[5] == 13
    assert out[9] == 25
    assert out[10] == 22


def test_qp_for_bits_rounds_to_nearest():
    """缺陷 7：向上取整而不是四舍五入，累计 QP 偏高、码率偏低。"""
    complexity = 1_000_000.0
    target_bits = complexity * 2 ** (-20.25 / 3)

    qp = qp_for_bits(target_bits, complexity, 1.0, 0.0)

    assert qp == 20
    assert frame_bits(complexity, qp, 1.0, 0.0) > target_bits
    assert frame_bits(complexity, 21, 1.0, 0.0) < target_bits


def test_vbv_level_uses_frame_rate_not_seconds():
    """缺陷 8：按秒而不是按帧更新，帧率变化时水位错误。"""
    at_30 = vbv_level(500_000.0, 0.0, 30.0, 3000.0)
    at_60 = vbv_level(500_000.0, 0.0, 60.0, 3000.0)

    assert at_30 == pytest.approx(400_000.0)
    assert at_60 == pytest.approx(450_000.0)
    assert at_60 == pytest.approx(500_000.0 - 3000.0 * 1000.0 / 60.0)


def test_validate_plan_reports_underflow():
    """缺陷 9：只检查上溢不检查下溢，缓冲区空转未被发现。"""
    budget = {
        "buffer_size": 800_000.0,
        "budget_bits": 3_000_000.0,
        "actual_bits": 2_999_500.0,
        "max_qp_step": 2,
    }

    assert validate_plan([20, 21, 22], [500_000.0, 450_000.0, 400_000.0], budget) == []
    # 水位恰好为 0 合法，只有小于 0 才是 underflow。
    assert validate_plan([20, 21, 22], [500_000.0, 0.0, 400_000.0], budget) == []

    issues = validate_plan(
        [20, 21, 22],
        [500_000.0, -1.0, 400_000.0],
        budget,
    )
    assert any("underflow" in issue.lower() for issue in issues)

    overflow = validate_plan(
        [20, 21, 22],
        [500_000.0, 900_000.0, 400_000.0],
        budget,
    )
    assert any("overflow" in issue.lower() for issue in overflow)


def test_control_batch_keeps_clips_independent():
    """缺陷 10：跨片复用缓冲区状态，上一片的水位泄漏到下一片。"""
    clips = [
        {"name": "a", "frames": [1_000_000.0] * 10, "budget_bits": 156_250.0},
        {"name": "b", "frames": [2_000_000.0] * 10, "budget_bits": 312_500.0},
    ]

    batch = control_batch(clips, CONFIG)

    assert batch["a"]["levels"][0] == pytest.approx(495_625.0)
    assert batch["b"]["levels"][0] == pytest.approx(511_250.0)
    assert batch["a"]["qps"] is not batch["b"]["qps"]

    batch["a"]["qps"][0] = 0
    assert batch["b"]["qps"][0] == 18


def test_constant_complexity_gop_hits_budget():
    """组合：恒定复杂度下 30 帧总比特等于预算（±1 kbps）。"""
    frames = [1_000_000.0] * 30
    budget_bits = 468_750.0
    config = {"k": 1.0, "c": 0.0, "fps": 30.0}

    qps = plan_gop(frames, budget_bits, config)
    total = sum(frame_bits(c, q, 1.0, 0.0) for c, q in zip(frames, qps))

    assert set(qps) == {18}
    assert total == pytest.approx(budget_bits, abs=1000.0)


def test_frame_bits_decreases_monotonically_with_qp():
    """组合：QP 增大时比特数单调减少。"""
    qps = list(range(0, 52, 3))
    bits = [frame_bits(500_000.0, qp, 1.0, 0.0) for qp in qps]

    assert all(upper > lower for upper, lower in zip(bits, bits[1:]))


def test_buffer_levels_stay_within_bounds():
    """组合：整个 GOP 的缓冲区水位始终在 [0, size]。"""
    size = 800_000.0
    level = 500_000.0
    levels = []
    for _ in range(30):
        bits = frame_bits(1_000_000.0, 18, 1.0, 0.0)  # 15625 bit
        level = vbv_level(level, bits, 30.0, 600.0)  # drain 20000 bit
        levels.append(level)

    assert all(0.0 <= item <= size for item in levels)
    assert levels[-1] == pytest.approx(368_750.0)


def test_scene_weighting_preserves_total_budget():
    """组合：场景加权前后总预算相同（±1 bit）。"""
    complexities = [1000.0, 1000.0, 3000.0, 3000.0, 1000.0]
    budget_bits = 1_000_000.0

    base = [budget_bits / len(complexities)] * len(complexities)
    weights = scene_bonus(complexities, 2.0)
    weighted = [bits * weight for bits, weight in zip(base, weights)]

    assert sum(base) == pytest.approx(budget_bits, abs=1.0)
    assert sum(weighted) == pytest.approx(budget_bits, abs=1.0)
    assert weighted[2] > base[2]


def test_two_clips_match_solo_control():
    """组合：两片批量控制结果与各自单独控制完全相同。"""
    clips = [
        {"name": "a", "frames": [1_000_000.0] * 10, "budget_bits": 156_250.0},
        {"name": "b", "frames": [2_000_000.0] * 10, "budget_bits": 312_500.0},
    ]

    batch = control_batch(clips, CONFIG)
    solo_a = control_batch([clips[0]], CONFIG)["a"]
    solo_b = control_batch([clips[1]], CONFIG)["b"]

    assert batch["a"] == solo_a
    assert batch["b"] == solo_b
    assert solo_a["final_level"] == pytest.approx(456_250.0)
    assert solo_b["final_level"] == pytest.approx(612_500.0)
