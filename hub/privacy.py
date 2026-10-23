"""외부 AI로 보내기 전에 개인정보를 토큰으로 가리고, 받은 초안에서 다시 되돌린다."""
from __future__ import annotations

import re

_PATTERNS = [
    ("주민번호", re.compile(r"\b\d{6}-?[1-4]\d{6}\b")),
    ("이메일", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    ("전화", re.compile(r"(?<!\d)(?:\+?82[-\s]?1[016789]|01[016789])[-\s.]?\d{3,4}[-\s.]?\d{4}(?!\d)")),
]
_UNKNOWN_TOKEN = re.compile(r"\[(?:고객|전화|이메일|주민번호)\d+\](?:님|様|さん)?,?[ \t]?")


class Masker:
    """한 문의 스레드 동안 같은 값은 같은 토큰([고객1], [전화1] ...)으로 바꾼다."""

    def __init__(self, known_names: list[str] | None = None, aliases: dict[str, str] | None = None):
        """aliases는 {다른 표기: 대표 이름}. 같은 사람은 표기가 달라도 같은 토큰을 받아야
        AI가 서로 다른 사람으로 오해하지 않는다 (예: '서연' → '박서연')."""
        self._token_by_value: dict[str, str] = {}
        self._value_by_token: dict[str, str] = {}
        self._counts: dict[str, int] = {}
        self._canonical = {n.strip(): n.strip() for n in known_names or [] if n and len(n.strip()) >= 2}
        # 영문 이름은 'Emily'처럼 이름만 쓰는 경우가 많아 부분도 같은 사람으로 가린다.
        for name in list(self._canonical):
            for part in name.split() if " " in name else []:
                if len(part) >= 3:
                    self._canonical.setdefault(part, name)
        for alias, name in (aliases or {}).items():
            if alias and len(alias.strip()) >= 2 and name:
                self._canonical[alias.strip()] = name.strip()
        # 긴 이름부터 바꿔야 'Emily Chen'보다 'Emily'가 먼저 잡히는 일이 없다.
        self._names = sorted(self._canonical, key=len, reverse=True)

    def token_for(self, kind: str, value: str) -> str:
        if value not in self._token_by_value:
            self._counts[kind] = self._counts.get(kind, 0) + 1
            token = f"[{kind}{self._counts[kind]}]"
            self._token_by_value[value] = token
            self._value_by_token[token] = value
        return self._token_by_value[value]

    def mask(self, text: str) -> str:
        if not text:
            return text
        for name in self._names:
            if name in text:
                text = text.replace(name, self.token_for("고객", self._canonical[name]))
        for kind, pattern in _PATTERNS:
            text = pattern.sub(lambda m, k=kind: self.token_for(k, m.group(0)), text)
        return text

    def unmask(self, text: str) -> str:
        for token, value in self._value_by_token.items():
            text = text.replace(token, value)
        # 로컬 모델이 이름 없는 고객에게 '[고객1]님'을 지어낸 경우가 있었다. 모르는 토큰은 고객에게 보이지 않게 지운다.
        return _UNKNOWN_TOKEN.sub("", text)

    def values(self, kind: str) -> list[str]:
        """가린 값 중 한 종류(예: '전화')만."""
        return [v for token, v in self._value_by_token.items() if token.startswith(f"[{kind}")]

    @property
    def replacements(self) -> int:
        return len(self._value_by_token)
