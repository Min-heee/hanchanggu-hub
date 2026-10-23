"""네이버 톡톡 챗봇 API 연동: 웹훅으로 문의를 받고, 실장이 승인한 답장을 보내기 API로 보낸다.

명세는 github.com/navertalk/chatbot-api (README, 이슈의 공식 답변)를 따랐다. 지킬 것:
- 웹훅은 200 말고는 모두 실패로 센다. 실패가 한 시간 안에 쌓이면 웹훅이 막히고 저절로 풀리지 않는다.
  그래서 받자마자 200으로 답하고, 처리 중 오류가 나도 로그만 남긴다. 네이버는 다시 보내 주지 않는다.
- 웹훅에는 서명이 없다. 주소에 비밀 경로를 넣고(NAVER_TALKTALK_WEBHOOK_TOKEN), 가능하면 네이버 IP 대역만 열어 둔다.
- user 값은 파트너 계정마다 다르다. 테스트 계정의 user로 실서비스 계정에 보내면 '권한없는 파트너에 대한 요청'(01)이 온다.
- 보내기 API 오류도 HTTP 200에 success:false로 온다. 코드보다 resultMessage를 본다('잘못된 user 값'이 02로도 99로도 왔다).
- 시간이 지난 뒤 사람이 답하는 방식은 네이버가 지원한다고 답했다(2026-09, 다른 병원 문의). 다만 같은 구성에서
  보내기가 99 '알 수 없는 오류'로 실패한 사례가 아직 열려 있어, 테스트 계정에서 보내기부터 확인해야 한다.
"""
from __future__ import annotations

import http.client
import json
import os
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass

from .models import InboundMessage

SEND_URL = "https://gw.talk.naver.com/chatbot/v1/event"
MAX_TEXT = 10_000
SENDER_NAME = "네이버 톡톡 사용자"  # 웹훅에는 이름이 없다. 프로필 API로 받을 수 있지만 동의 절차 동안 보내기가 막힌다.


class TalkTalkError(ValueError):
    pass


def message_id(user: str, payload: dict) -> str:
    """messageId는 문서에 없는 대화별 순번이다(숫자나 문자열). user와 묶어야 중복을 가릴 수 있다."""
    seq = payload.get("messageId")
    return f"talktalk:{user}:{seq}" if seq not in (None, "") else f"talktalk:{user}:{uuid.uuid4().hex[:12]}"


def _text_of(payload: dict) -> str:
    text = (payload.get("textContent") or {}).get("text") or ""  # 파일과 함께 빈 textContent가 올 때가 있다
    if text:
        return text
    if image := payload.get("imageContent"):
        return f"[사진을 보냈어요] {image.get('imageUrl', '')}".strip()
    if (file := payload.get("fileContent")) and file.get("fileName"):
        return f"[파일을 보냈어요] {file['fileName']}"
    return ""


def parse_inbound(payload: dict, received_at: str) -> InboundMessage | None:
    """고객이 보낸 메시지(send)만 문의로 바꾼다. 웹훅에는 보낸 시각이 없어 받은 시각을 쓴다."""
    if payload.get("event") != "send" or not payload.get("user"):
        return None
    text = _text_of(payload)
    if not text:
        return None
    user = str(payload["user"])
    return InboundMessage(
        id=message_id(user, payload), channel="naver", channel_user_id=user,
        sender_name=SENDER_NAME, text=text, received_at=received_at,
    )


@dataclass
class AgentReply:
    """파트너센터에서 상담원이 직접 보낸 답장(echo). 한창구 밖에서 답했으니 문의함도 '답장함'으로 맞춘다."""

    user: str
    text: str
    manager: str


def parse_agent_echo(payload: dict) -> AgentReply | None:
    options = payload.get("options") or {}
    # managerNickname은 상담원이 직접 입력한 메시지에만 있다. 보내기 API로 보낸 우리 답장의 echo는 건너뛴다.
    if payload.get("event") != "echo" or payload.get("echoedEvent") != "send" or not options.get("managerNickname"):
        return None
    text = _text_of(payload)
    return AgentReply(str(payload["user"]), text, str(options["managerNickname"])) if text and payload.get("user") else None


_ERRORS = {
    "01": "인증 키가 맞지 않거나, 이 고객과 대화한 적 없는 계정이에요. 파트너센터의 보내기 API 키를 그대로 넣었는지, "
          "테스트 계정의 고객에게 실서비스 키로 보내지 않았는지 확인해 주세요.",
    "02": "보낼 내용이나 고객 값이 잘못됐어요.",
}


class TalkTalkClient:
    def __init__(self, auth_key: str, url: str = SEND_URL, timeout: float = 10):
        self.auth_key = auth_key
        self.url = url
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> TalkTalkClient | None:
        key = os.environ.get("NAVER_TALKTALK_AUTH_KEY")
        return cls(key, os.environ.get("NAVER_TALKTALK_SEND_URL") or SEND_URL) if key else None

    def send_text(self, user: str, text: str) -> None:
        if len(text) > MAX_TEXT:
            raise TalkTalkError(f"네이버 톡톡은 한 번에 {MAX_TEXT:,}자까지 보낼 수 있어요.")
        # partner 필드는 넣지 않는다(넣으면 '권한없는 파트너에 대한 요청'이 났다는 공식 답변).
        # notification: 몇 시간 뒤 보내는 답장은 사용자가 대화방에 없을 때가 많아 푸시를 켠다.
        body = {"event": "send", "user": user, "textContent": {"text": text}, "options": {"notification": True}}
        request = urllib.request.Request(
            self.url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json;charset=UTF-8", "Authorization": self.auth_key},  # 키 그대로, Bearer 없이
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.load(response)
        except urllib.error.HTTPError as e:
            raise TalkTalkError(f"네이버 톡톡 서버 오류예요 (HTTP {e.code}). 잠시 뒤 다시 보내 주세요.") from e
        except (urllib.error.URLError, TimeoutError) as e:
            raise TalkTalkError("네이버 톡톡 서버에 연결하지 못했어요. 잠시 뒤 다시 보내 주세요.") from e
        except (http.client.HTTPException, OSError, json.JSONDecodeError) as e:
            # 요청은 갔는데 응답을 못 받았을 수 있다. 다시 보내면 두 번 갈 수 있으니 먼저 확인하게 한다
            raise TalkTalkError("보냈는지 알 수 없어요. 파트너센터 대화방에서 확인한 뒤 필요하면 다시 보내 주세요.") from e
        if not isinstance(data, dict):
            raise TalkTalkError("보냈는지 알 수 없어요. 파트너센터 대화방에서 확인한 뒤 필요하면 다시 보내 주세요.")

        if data.get("success") is True:
            return
        code, detail = str(data.get("resultCode", "")), str(data.get("resultMessage", ""))
        if "잘못된 user" in detail:
            raise TalkTalkError("이 고객의 네이버 톡톡 계정을 찾지 못했어요. 다른 파트너 계정(테스트/실서비스)의 고객일 수 있어요.")
        if "보낼 수 없음" in detail:
            raise TalkTalkError("고객이 정보 제공 동의 화면에 있어 지금은 보낼 수 없어요. 1~2분 뒤 다시 보내 주세요.")
        if code == "99":  # 사람이 나중에 답하는 구성에서 보고된 미해결 오류. 답장이 묶이지 않게 다른 길을 알려 준다
            raise TalkTalkError(f"네이버 톡톡이 보내지 못했어요: {detail or '알 수 없는 오류'} (코드 99). 이번 답장은 파트너센터에서 직접 보내 주세요.")
        raise TalkTalkError(f"네이버 톡톡이 보내지 못했어요: {_ERRORS.get(code, detail or '알 수 없는 오류')} (코드 {code})")
