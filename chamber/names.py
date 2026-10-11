"""Имена файлов стола — латиницей (наказ Автора 2026-09-10: проект
международный). Одна транслитерация для всего: имя раунда в имени файла,
старые кириллические имена в записях журнала (seed_file) — через карту
префиксов, чтобы catchup находил затравку по старой записи."""
from __future__ import annotations

import re

_TR = {'а':'a','б':'b','в':'v','г':'g','д':'d','е':'e','ё':'yo','ж':'zh','з':'z','и':'i','й':'y',
       'к':'k','л':'l','м':'m','н':'n','о':'o','п':'p','р':'r','с':'s','т':'t','у':'u','ф':'f',
       'х':'kh','ц':'ts','ч':'ch','ш':'sh','щ':'shch','ъ':'','ы':'y','ь':'','э':'e','ю':'yu','я':'ya'}
# старый префикс файла раунда → новый (файлы до 2026-09-10 переименованы так же)
LEGACY_PREFIX = {"ВОПРОС-": "QUESTION-", "ЗАТРАВКА-": "SEED-", "СВОД-": "SUMMARY-",
                 "ДОСЬЕ-": "DOSSIER-", "ответы-": "answers-"}


def translit(s: str) -> str:
    out = []
    for ch in s:
        lo = ch.lower()
        if lo in _TR:
            t = _TR[lo]
            out.append(t.upper() if ch.isupper() and t else t)
        else:
            out.append(ch)
    return "".join(out)


_BAD = re.compile(r"[/\\\x00-\x1f\x7f*?\[\]]|(?:^|/)\.\.?(?:/|$)")


def round_file(prefix: str, round_name: str, ext: str = ".md") -> str:
    """Имя файла раунда: QUESTION-/SEED-/SUMMARY- + имя раунда латиницей.

    Имя раунда — одна компонента пути: разделители, `..`, управляющие и
    glob-символы отвергаются (мини-ревизия: codex, deepseek — иначе
    `x/../../outside` вывел бы затравку за каталог раундов). Известная
    граница: транслитерация не взаимно-однозначна (ё/yo, ь и ъ
    опускаются, е/э → e) — два имени, различающиеся только этим, дали бы
    один файл; окно ограничивает имена регуляркой, а такие пары в журнале
    не встречались."""
    if not round_name or _BAD.search(round_name):
        raise ValueError(f"имя раунда не годится для файла: {round_name!r}")
    body = translit(round_name)
    if not body:                                    # «ьъ» → пусто
        raise ValueError(f"имя раунда пусто после транслитерации: {round_name!r}")
    return f"{prefix}{body}{ext}"


def legacy_to_new(name: str) -> str:
    """Старое имя файла (кириллический префикс и/или имя раунда) → новое.
    Берётся только basename: путь из записи журнала мог быть абсолютным."""
    name = name.rsplit("/", 1)[-1]
    for old, new in LEGACY_PREFIX.items():
        if name.startswith(old):
            return new + translit(name[len(old):])
    return translit(name)
