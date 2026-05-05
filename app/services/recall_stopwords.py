"""召回停用词：内置默认 + data/.pathy 可配置覆盖。"""

from __future__ import annotations

from pathlib import Path

from app.config import Settings

# 体量适中：覆盖常见虚词/泛词；可按业务再追加。
DEFAULT_STOPWORDS: frozenset[str] = frozenset(
    {
        # English
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "with",
        "by",
        "from",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "must",
        "can",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "i",
        "you",
        "he",
        "she",
        "we",
        "they",
        "what",
        "which",
        "who",
        "when",
        "where",
        "why",
        "how",
        "all",
        "each",
        "every",
        "both",
        "few",
        "more",
        "most",
        "other",
        "some",
        "such",
        "no",
        "nor",
        "not",
        "only",
        "own",
        "same",
        "so",
        "than",
        "too",
        "very",
        "just",
        "also",
        "now",
        "here",
        "there",
        "then",
        "if",
        "about",
        "into",
        "through",
        "during",
        "before",
        "after",
        "above",
        "below",
        "between",
        "under",
        "again",
        "further",
        "once",
        "any",
        "me",
        "him",
        "her",
        "us",
        "them",
        "my",
        "your",
        "his",
        "our",
        "their",
        # 常见英文技术泛词（可按需删）
        "api",
        "app",
        # 中文（高频虚词、疑问与口语）
        "的",
        "了",
        "和",
        "是",
        "在",
        "有",
        "就",
        "不",
        "人",
        "都",
        "一",
        "一个",
        "上",
        "也",
        "很",
        "到",
        "说",
        "要",
        "去",
        "你",
        "会",
        "着",
        "没有",
        "看",
        "好",
        "自己",
        "这",
        "那",
        "这个",
        "那个",
        "这样",
        "这样",
        "什么",
        "怎么",
        "为什么",
        "哪",
        "哪些",
        "哪里",
        "谁",
        "几",
        "多",
        "少",
        "能",
        "可以",
        "应该",
        "如果",
        "因为",
        "所以",
        "但是",
        "而且",
        "或者",
        "还是",
        "还是",
        "与",
        "及",
        "等",
        "之",
        "为",
        "以",
        "对",
        "从",
        "把",
        "被",
        "让",
        "向",
        "将",
        "已",
        "还",
        "又",
        "再",
        "更",
        "最",
        "请",
        "问",
        "想",
        "知道",
        "告诉",
        "一下",
        "如何",
        "怎样",
        "是否",
        "有没有",
        "吗",
        "呢",
        "吧",
        "啊",
        "嘛",
        "呀",
        "哦",
        "嗯",
        "哈",
        "唉",
        "哎",
        "的话",
        "来说",
        "来说",
        "方面",
        "时候",
        "情况",
        "问题",
        "内容",
        "东西",
        "进行",
        "通过",
        "使用",
        "需要",
        "认为",
        "觉得",
        "觉得",
        "希望",
        "帮助",
        "谢谢",
        "感谢",
    }
)

_RUNTIME_STOPWORDS_REL = ".pathy/recall_stopwords.txt"


def runtime_stopwords_path(settings: Settings) -> Path:
    return settings.data_root.resolve() / _RUNTIME_STOPWORDS_REL


def parse_stopwords_text(text: str) -> list[str]:
    """按行解析停用词；忽略空行与 # 注释；统一小写并去重。"""
    out: list[str] = []
    seen: set[str] = set()
    for raw in (text or "").splitlines():
        w = raw.strip().lower()
        if not w or w.startswith("#"):
            continue
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out


def read_runtime_stopwords(settings: Settings) -> list[str]:
    p = runtime_stopwords_path(settings)
    if not p.is_file():
        return []
    return parse_stopwords_text(p.read_text(encoding="utf-8"))


def read_effective_stopwords(settings: Settings) -> list[str]:
    runtime_words = read_runtime_stopwords(settings)
    if runtime_words:
        return runtime_words
    return sorted(DEFAULT_STOPWORDS)


def write_runtime_stopwords(settings: Settings, words: list[str]) -> tuple[int, str]:
    p = runtime_stopwords_path(settings)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Recall stopwords (one per line)", "# Empty file means fallback to built-in defaults", ""]
    lines.extend(words)
    text = "\n".join(lines).rstrip() + "\n"
    p.write_text(text, encoding="utf-8")
    return len(words), _RUNTIME_STOPWORDS_REL
