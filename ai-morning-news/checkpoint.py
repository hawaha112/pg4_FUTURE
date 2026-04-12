"""
checkpoint.py — 流水线断点续跑支持

存储结构（JSON）：
{
  "stage": "rss_done" | "enrich_done" | "llm_partial" | "llm_done",
  "created_at": "ISO timestamp",
  "items": [...],
  "llm_progress": { "0": {...}, "3": {...}, ... }
}

用法:
    ckpt = PipelineCheckpoint("pipeline.ckpt.json")
    if ckpt.exists():
        data = ckpt.load()
    ckpt.save("rss_done", items)
    ckpt.save_llm_result(idx, analysis)
    ckpt.remove()
"""

import json
from datetime import datetime, timezone
from pathlib import Path


class PipelineCheckpoint:
    """流水线 checkpoint 管理器，支持断点续跑。"""

    def __init__(self, path):
        self.path = Path(path)
        self._data = None

    def exists(self):
        return self.path.exists()

    def load(self):
        """加载 checkpoint，还原 datetime 字段"""
        with open(self.path, 'r', encoding='utf-8') as f:
            self._data = json.load(f)
        # 还原 datetime
        for item in self._data.get('items', []):
            if item.get('published'):
                try:
                    item['published'] = datetime.fromisoformat(item['published'])
                except (ValueError, TypeError):
                    item['published'] = None
        return self._data

    def save(self, stage, items, llm_progress=None):
        """保存 checkpoint，自动序列化 datetime"""
        serialized = []
        for item in items:
            item_copy = dict(item)
            if isinstance(item_copy.get('published'), datetime):
                item_copy['published'] = item_copy['published'].isoformat()
            serialized.append(item_copy)
        self._data = {
            'stage': stage,
            'created_at': datetime.now(timezone.utc).isoformat(),
            'items': serialized,
            'llm_progress': llm_progress or {},
        }
        # 原子写入（先写临时文件再 rename，防止写到一半断电）
        tmp = self.path.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self._data, f, ensure_ascii=False)
        tmp.rename(self.path)

    def save_llm_result(self, idx, analysis):
        """增量保存单条 LLM 分析结果（不重写全部数据）"""
        if self._data is None:
            if self.exists():
                with open(self.path, 'r', encoding='utf-8') as f:
                    self._data = json.load(f)
            else:
                return
        self._data.setdefault('llm_progress', {})[str(idx)] = analysis
        self._data['stage'] = 'llm_partial'
        tmp = self.path.with_suffix('.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self._data, f, ensure_ascii=False)
        tmp.rename(self.path)

    def get_llm_progress(self):
        """获取已完成的 LLM 分析索引和结果"""
        if self._data is None:
            return {}
        return {int(k): v for k, v in self._data.get('llm_progress', {}).items()}

    def get_stage(self):
        if self._data:
            return self._data.get('stage', '')
        return ''

    def remove(self):
        """清理 checkpoint 文件。永不抛异常——即使文件系统只读或无权限，
        最多把内容清空（下次 exists() 仍为 True 但 load() 会拿到空 data），
        也不让流水线末尾因为清理失败而崩溃。"""
        import logging
        log = logging.getLogger('checkpoint')
        for p in (self.path, self.path.with_suffix('.tmp')):
            if not p.exists():
                continue
            try:
                p.unlink()
            except (PermissionError, OSError) as e:
                # 兜底：尝试把文件截断为空，避免下次误恢复
                try:
                    with open(p, 'w', encoding='utf-8') as f:
                        f.write('{}')
                    log.warning("checkpoint unlink 失败，已清空内容: %s (%s)", p, e)
                except Exception as e2:
                    log.warning("checkpoint 清理失败（已忽略）: %s (%s)", p, e2)
