"""
health_tracker.py — Source health monitoring

SourceHealthTracker class for tracking feed fetching health metrics:
- Response times
- Success/failure rates
- Consecutive failures
- Alert generation for failing sources
"""

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from logger import get_logger

log = get_logger('health_tracker')


class SourceHealthTracker:
    """跟踪每个源的抓取健康状态，含响应时间、成功率等指标，连续失败超过阈值时报警"""

    def __init__(self, health_path: str, alert_threshold: int = 3):
        self.health_path = Path(health_path)
        self.alert_threshold = alert_threshold
        self.data = self._load()
        # 运行时计时器
        self._timers: dict = {}

    def _load(self) -> dict:
        if self.health_path.exists():
            try:
                with open(self.health_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def save(self):
        try:
            with open(self.health_path, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except OSError as e:
            log.warning("⚠️ 健康度数据保存失败: %s", e)

    def start_timer(self, source_name: str):
        """开始计时（在 fetch 之前调用）"""
        self._timers[source_name] = time.time()

    def _get_elapsed(self, source_name: str) -> float:
        """获取耗时（秒），没有计时器返回 0"""
        start = self._timers.pop(source_name, None)
        if start is not None:
            return round(time.time() - start, 2)
        return 0.0

    def record_success(self, source_name: str, item_count: int):
        prev = self.data.get(source_name, {})
        elapsed = self._get_elapsed(source_name)

        # 计算滚动平均响应时间（指数移动平均 α=0.3）
        prev_avg = prev.get('avg_response_time', 0.0)
        if prev_avg > 0 and elapsed > 0:
            avg_time = round(prev_avg * 0.7 + elapsed * 0.3, 2)
        else:
            avg_time = elapsed

        # 成功计数
        total_successes = prev.get('total_successes', 0) + 1
        total_runs = prev.get('total_runs', 0) + 1

        self.data[source_name] = {
            'status': 'ok',
            'last_success': datetime.now(timezone.utc).isoformat(),
            'last_count': item_count,
            'consecutive_failures': 0,
            'last_error': '',
            'last_response_time': elapsed,
            'avg_response_time': avg_time,
            'total_successes': total_successes,
            'total_runs': total_runs,
            'success_rate': round(total_successes / total_runs * 100, 1) if total_runs > 0 else 100.0,
        }

    def record_failure(self, source_name: str, error: str):
        prev = self.data.get(source_name, {})
        failures = prev.get('consecutive_failures', 0) + 1
        elapsed = self._get_elapsed(source_name)

        total_runs = prev.get('total_runs', 0) + 1
        total_successes = prev.get('total_successes', 0)

        self.data[source_name] = {
            'status': 'failing',
            'last_success': prev.get('last_success', ''),
            'last_count': prev.get('last_count', 0),
            'consecutive_failures': failures,
            'last_error': str(error)[:200],
            'last_response_time': elapsed,
            'avg_response_time': prev.get('avg_response_time', 0.0),
            'total_successes': total_successes,
            'total_runs': total_runs,
            'success_rate': round(total_successes / total_runs * 100, 1) if total_runs > 0 else 0.0,
        }

    def should_skip(self, source_name: str, auto_skip_threshold: int = 10) -> bool:
        """检查源是否应由于连续失败而自动跳过。

        Args:
            source_name: 源名称
            auto_skip_threshold: 连续失败的自动跳过阈值（默认 10 次）

        Returns:
            True 表示应跳过，False 表示应尝试抓取

        说明：
        - 连续失败 >= auto_skip_threshold 时自动跳过
        - 但允许每 24 小时重试一次（检测源是否恢复）
        """
        info = self.data.get(source_name, {})
        failures = info.get('consecutive_failures', 0)
        if failures < auto_skip_threshold:
            return False

        # 允许每天重试一次，即使源已被自动跳过
        last_success = info.get('last_success', '')
        if last_success:
            try:
                last_dt = datetime.fromisoformat(last_success)
                # 检查是否有最近的自动跳过重试记录
                last_retry = info.get('last_auto_skip_retry', '')
                if last_retry:
                    retry_dt = datetime.fromisoformat(last_retry)
                    hours_since_retry = (datetime.now(timezone.utc) - retry_dt).total_seconds() / 3600
                    if hours_since_retry < 24:
                        return True  # 在 24 小时内已重试过，继续跳过
                # 没有最近的重试记录 — 允许本次尝试，并记录时间戳
                info['last_auto_skip_retry'] = datetime.now(timezone.utc).isoformat()
                return False
            except (ValueError, TypeError):
                pass

        return True

    def get_alerts(self) -> list:
        """返回连续失败超过阈值的源列表"""
        alerts = []
        for name, info in self.data.items():
            fails = info.get('consecutive_failures', 0)
            if fails >= self.alert_threshold:
                last_ok = info.get('last_success', '从未成功')
                err = info.get('last_error', '未知错误')
                alerts.append({
                    'source': name,
                    'consecutive_failures': fails,
                    'last_success': last_ok,
                    'last_error': err,
                    'success_rate': info.get('success_rate', 0),
                })
        return alerts

    def print_report(self):
        alerts = self.get_alerts()
        if not alerts:
            return
        log.warning("🚨 源健康度警报：%d 个源连续失败", len(alerts))
        for a in alerts:
            last_ok = a['last_success'][:10] if a['last_success'] != '从未成功' else '从未成功'
            auto_skip_status = ""
            # 检查是否已达到自动跳过阈值（默认 10 次）
            if a['consecutive_failures'] >= 10:
                auto_skip_status = " [自动跳过中]"
            log.warning("⛔ %s: 连续 %d 次失败 | 成功率 %.0f%% | 上次成功: %s | 错误: %s%s",
                        a['source'], a['consecutive_failures'],
                        a.get('success_rate', 0), last_ok, a['last_error'][:60], auto_skip_status)

    def get_dead_sources(self, disable_threshold: int = 20) -> list:
        """返回连续失败次数 >= disable_threshold 的源名列表，调用方可据此自动禁用。"""
        return [
            name for name, info in self.data.items()
            if info.get('consecutive_failures', 0) >= disable_threshold
        ]

    def auto_disable_dead_sources(self, config_path: str,
                                  disable_threshold: int = 20) -> list:
        """把连续失败 >= disable_threshold 的源在 config.json 中标为 enabled=false。

        Args:
            config_path: config.json 路径
            disable_threshold: 自动禁用阈值

        Returns:
            被本次自动禁用的源名列表（空表示无操作）

        说明：
        - 只处理 sources.english / sources.chinese 两大列表里的 enabled != false 且仍失败的源
        - 禁用后在对应条目上写一个 _auto_disabled_at 时间戳，便于事后审查 / 恢复
        """
        disabled_now = []
        if not Path(config_path).exists():
            return disabled_now
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.warning("⚠️ 读取 config.json 失败，跳过自动禁用: %s", e)
            return disabled_now

        dead_set = set(self.get_dead_sources(disable_threshold=disable_threshold))
        if not dead_set:
            return disabled_now

        sources = cfg.get('sources', {})
        for lang_key in ('english', 'chinese'):
            lst = sources.get(lang_key, [])
            if not isinstance(lst, list):
                continue
            for src in lst:
                name = src.get('name', '')
                if (name in dead_set
                        and src.get('enabled', True) is not False):
                    src['enabled'] = False
                    src['_auto_disabled_at'] = datetime.now(timezone.utc).isoformat()
                    src['_auto_disabled_reason'] = (
                        f"连续失败 {self.data[name].get('consecutive_failures', 0)} 次"
                    )
                    disabled_now.append(name)

        if disabled_now:
            # 写回 config.json
            try:
                with open(config_path, 'w', encoding='utf-8') as f:
                    json.dump(cfg, f, ensure_ascii=False, indent=2)
                log.warning("🛑 已自动禁用 %d 个长期失败的源: %s",
                            len(disabled_now), ', '.join(disabled_now))
            except OSError as e:
                log.warning("⚠️ 写回 config.json 失败: %s", e)
                disabled_now = []  # 未落盘，视为未禁用
        return disabled_now
