"""웹 API와 화면. 실행: uvicorn hub.app:create_app --factory --port 8765"""
from __future__ import annotations

import hmac
import json
import logging
import os
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, Header, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import compliance
from .channels import ChannelRouter, MockChannels
from .crm import CsvCRM, SqliteCRM
from .drafting.local import LocalDrafter
from .drafting.rules import RuleDrafter
from .knowledge import KnowledgeBase
from .models import now_iso
from .pipeline import Hub, StaleThread
from .recall import RecallEngine, RecallStore, load_rules
from .recall.writer import ClaudeRecallWriter, LocalRecallWriter
from .store import Store
from .talktalk import TalkTalkClient, parse_agent_echo, parse_inbound
from .webhooks import parse_kakao_channel, parse_nhn_unsubscribe

ROOT = Path(__file__).resolve().parent.parent
log = logging.getLogger("hanchanggu")
DATA = ROOT / "data"


def load_env_file(path: Path) -> None:
    """KEY=VALUE 형식의 .env를 읽는다. 이미 셸에 설정된 값은 덮어쓰지 않는다."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if value and not os.environ.get(key.strip()):
            os.environ[key.strip()] = value


def build_hub(runtime: Path | None = None, crm_kind: str | None = None, llm: str | None = None, today: date | None = None) -> Hub:
    load_env_file(ROOT / ".env")
    runtime = runtime or DATA / "runtime"
    # today를 넘기지 않으면 Hub·리콜·데모 채널이 그때그때 한국 시간 날짜를 쓴다 (웹훅 앱은 며칠씩 켜 둔다)
    crm_kind = crm_kind or os.environ.get("CRM_ADAPTER", "sqlite")
    # 유료 API는 명시적으로 켰을 때만 쓴다. 키가 있다는 이유만으로 과금되지 않게 한다.
    llm = llm or os.environ.get("INQUIRY_HUB_LLM") or "offline"

    crm = CsvCRM(runtime / "csv_crm") if crm_kind == "csv" else SqliteCRM(runtime / "crm.db")
    if llm == "local":
        drafter = LocalDrafter()
    elif llm == "claude":
        from .drafting.claude import ClaudeDrafter

        drafter = ClaudeDrafter()
    else:
        drafter = RuleDrafter()

    return Hub(
        store=Store(runtime / "inbox.db"),
        crm=crm,
        # 네이버 톡톡 보내기 API 키가 있으면 톡톡 웹훅으로 들어온 대화의 답장은 실제로 보낸다. 나머지는 데모 outbox.
        channels=ChannelRouter(MockChannels(DATA / "inquiries_seed.json", runtime / "outbox.jsonl", today), TalkTalkClient.from_env()),
        knowledge=KnowledgeBase.load(DATA / "knowledge"),
        drafter=drafter,
        crm_seed=DATA / "crm_seed.json",
        today=today,
    )


def build_recall(hub: Hub) -> RecallEngine:
    """문의함과 같은 CRM·채널·안내 자료를 쓰고, 문의함 상태를 보고 보낼지 말지 정한다."""
    writer = None
    if hub.mode == "local":
        writer = LocalRecallWriter(hub.drafter.client)
    elif hub.mode == "claude":
        writer = ClaudeRecallWriter()
    engine = RecallEngine(
        crm=hub.crm,
        inbox=hub.store,
        channels=hub.channels,
        knowledge=hub.knowledge,
        rules=load_rules(DATA / "recall_rules.json"),
        store=RecallStore(hub.store.path.parent / "recall.db"),
        writer=writer,
        today=hub._today,
    )
    hub.optout.listeners.append(engine.on_consent_change)  # 수신거부가 들어오면 준비 중인 광고를 바로 멈춘다
    return engine


class ApproveBody(BaseModel):
    text: str
    handled_by: str = "상담실장"
    last_seen: str | None = None  # 화면에서 본 마지막 고객 메시지 ID


class RecallApproveBody(BaseModel):
    text: str | None = None
    handled_by: str = "상담실장"


class PlanBody(BaseModel):
    offset_days: int = 0


class SkipBody(BaseModel):
    reason: str = ""


class RecallCheckBody(BaseModel):
    text: str


class EscalateBody(BaseModel):
    note: str


class StaffOptOutBody(BaseModel):
    handled_by: str = "상담실장"


class CheckBody(BaseModel):
    text: str
    ad: bool = False


def _same(given: str | None, expected: str, header: bool = False) -> bool:
    """비밀 값 비교. 바이트로 비교해야 한글 같은 비ASCII 값에서도 예외(500) 없이 동작한다.
    HTTP 헤더는 latin-1로 풀려 들어오므로 원래 바이트로 되돌려 UTF-8 값과 비교한다."""
    raw = (given or "").encode("latin-1", "replace") if header else (given or "").encode()
    return hmac.compare_digest(raw, expected.encode())


def nhn_results(hub: Hub, payload: dict) -> list:
    return [hub.optout.from_080(u.phone, at=u.at) for u in parse_nhn_unsubscribe(payload)]


def kakao_results(hub: Hub, payload: dict) -> list:
    event = parse_kakao_channel(payload)
    return [hub.optout.from_kakao(event.user_id, event.blocked, at=event.at)] if event else []


def webhook_routes(hub: Hub, public: bool) -> APIRouter:
    """외부 서비스 → 한창구.

    public=True(인터넷에 여는 웹훅 전용 앱): 검증 값이 없으면 그 웹훅은 꺼져 있고, 응답에 고객 이름·ID를 넣지 않는다.
    public=False(내 컴퓨터의 본 앱): 검증 값을 정했으면 확인하고, 안 정했으면 받는다(로컬 시험용).
    """
    router = APIRouter(prefix="/webhooks")

    def summary(results: list) -> dict:
        if public:  # 번호만 알면 이름을 알아낼 수 있으면 안 된다
            return {"results": [{"changed": r.changed, "stopped": r.stopped} for r in results]}
        return {"results": [asdict(r) for r in results]}

    def check(header: str | None, expected: str | None, scheme: str = "") -> None:
        if not expected:
            if public:
                raise HTTPException(404)
            return
        if not _same(header, f"{scheme}{expected}", header=True):
            raise HTTPException(401, "검증 값이 맞지 않아요.")

    @router.post("/naver-talktalk/{token}")
    async def naver_talktalk_webhook(token: str, request: Request) -> Response:
        """네이버 톡톡 챗봇 API 웹훅. 서명이 없어 주소의 비밀 경로로 확인한다. 토큰을 정하지 않으면 꺼져 있다."""
        expected = os.environ.get("NAVER_TALKTALK_WEBHOOK_TOKEN")
        if not expected or not _same(token, expected):
            raise HTTPException(404)
        # 200이 아니면 네이버가 실패로 쌓다가 웹훅을 막는다(저절로 안 풀림). 처리 오류는 로그만 남기고 200으로 답한다.
        try:
            payload = json.loads(await request.body())
            if message := parse_inbound(payload, now_iso()):
                await run_in_threadpool(hub.ingest, [message], False)
            elif reply := parse_agent_echo(payload):
                await run_in_threadpool(hub.record_agent_reply, "naver", reply.user, reply.text, reply.manager)
        except Exception:
            log.exception("네이버 톡톡 웹훅을 처리하지 못했어요.")
        return Response(status_code=200)

    @router.post("/nhn-sms")
    def nhn_sms_webhook(payload: dict, x_toast_webhook_signature: str | None = Header(default=None)) -> dict:
        """NHN Cloud SMS 웹훅. 080 수신거부(UNSUBSCRIBE)를 CRM에 반영한다. 웹훅을 설정할 때 넣은 서명 값이 헤더로 온다."""
        check(x_toast_webhook_signature, os.environ.get("NHN_WEBHOOK_SIGNATURE"))
        return summary(nhn_results(hub, payload))

    @router.post("/kakao-channel")
    def kakao_channel_webhook(payload: dict, authorization: str | None = Header(default=None)) -> dict:
        """카카오톡 채널 웹훅. 채널 차단은 광고 수신거부로, 다시 추가는 채널 친구 여부만 반영한다. 3초 안에 2xx로 답해야 한다."""
        check(authorization, os.environ.get("KAKAO_ADMIN_KEY"), "KakaoAK ")
        return summary(kakao_results(hub, payload))

    return router


def create_webhook_app() -> FastAPI:
    """인터넷에 열 웹훅 전용 앱. 문의함 화면·API는 없고, 본 앱과 같은 runtime 폴더(DB)를 쓴다.

    실행: uvicorn hub.app:create_webhook_app --factory --port 8780 → 이 포트만 터널(cloudflared 등)로 연다.
    """
    hub = build_hub()
    hub.ensure_seeded()
    build_recall(hub)  # 수신거부가 들어오면 준비·예약 중인 광고를 멈추는 리스너를 건다
    app = FastAPI(title="한창구 웹훅", docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(webhook_routes(hub, public=True))
    return app


def create_app(hub: Hub | None = None, recall: RecallEngine | None = None) -> FastAPI:
    hub = hub or build_hub()
    hub.ensure_seeded()
    recall = recall or build_recall(hub)
    app = FastAPI(title="한창구")

    @app.middleware("http")
    async def same_site_only(request, call_next):
        """다른 사이트의 페이지가 이 화면 몰래 초기화·발송·수신거부를 일으키지 못하게 한다(브라우저가 붙이는 헤더로 판단).
        외부 서비스의 웹훅은 이 헤더를 보내지 않으므로 영향이 없다."""
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            site = request.headers.get("sec-fetch-site")
            origin = request.headers.get("origin")
            foreign_origin = origin and (origin == "null" or urlsplit(origin).netloc != request.headers.get("host"))
            if site in ("cross-site", "same-site") or foreign_origin:
                return JSONResponse({"detail": "다른 사이트에서 온 요청은 받지 않아요."}, status_code=403)
        return await call_next(request)

    @app.middleware("http")
    async def revalidate_static(request, call_next):
        response = await call_next(request)
        if not request.url.path.startswith("/api/"):  # 화면 파일을 고친 뒤 브라우저에 옛 JS가 남지 않게 매번 확인한다 (바뀌지 않았으면 304)
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    def view_or_404(key: str) -> dict:
        view = hub.thread_view(key)
        if view is None:
            raise HTTPException(404, "문의를 찾을 수 없어요.")
        return view

    @app.get("/api/status")
    def status() -> dict:
        return hub.status()

    @app.get("/api/threads")
    def threads() -> list[dict]:
        return hub.thread_list()

    @app.get("/api/threads/{key}")
    def thread(key: str) -> dict:
        return view_or_404(key)

    @app.post("/api/sync")
    def sync() -> dict:
        return hub.sync(wait_for_drafts=False)

    @app.post("/api/threads/{key}/regenerate")
    def regenerate(key: str) -> dict:
        view_or_404(key)
        hub.analyze(key)
        return view_or_404(key)

    @app.post("/api/threads/{key}/approve")
    def approve(key: str, body: ApproveBody) -> dict:
        view_or_404(key)
        if not body.text.strip():
            raise HTTPException(400, "보낼 내용이 비어 있어요.")
        try:
            result = hub.approve(key, body.text.strip(), body.handled_by, body.last_seen)
        except StaleThread as e:
            raise HTTPException(409, str(e)) from e
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {**result, "thread": view_or_404(key)}

    @app.post("/api/threads/{key}/escalate")
    def escalate(key: str, body: EscalateBody) -> dict:
        view_or_404(key)
        hub.escalate(key, body.note.strip() or "담당자 확인 필요")
        return view_or_404(key)

    @app.post("/api/check")
    def check(body: CheckBody) -> dict:
        flags = compliance.check(body.text)
        if body.ad:
            flags += compliance.check_ad_format(body.text, hub.clinic_name)
        return {"flags": flags}

    app.include_router(webhook_routes(hub, public=False))

    @app.post("/api/demo/webhooks/{kind}")
    def demo_webhook(kind: str, payload: dict) -> dict:
        """CRM 패널의 시연 버튼. 실제 서비스와 같은 모양의 본문을 검증 없이 처리한다 (내 컴퓨터의 본 앱에만 있다)."""
        handlers = {"nhn-sms": nhn_results, "kakao-channel": kakao_results}
        if kind not in handlers:
            raise HTTPException(404)
        return {"results": [asdict(r) for r in handlers[kind](hub, payload)]}

    @app.post("/api/customers/{customer_id}/opt-out")
    def staff_opt_out(customer_id: str, body: StaffOptOutBody) -> dict:
        """상담실장이 고객 뜻을 확인하고 광고 수신거부를 직접 반영한다."""
        result = hub.optout.from_staff(customer_id, body.handled_by)
        if result.customer_id is None:
            raise HTTPException(404, result.message)
        return asdict(result)

    @app.post("/api/reset")
    def reset() -> dict:
        recall.reset()
        return hub.reset(wait_for_drafts=False)

    # ---- 리콜·팔로업 --------------------------------------------------------

    def target_day(offset_days: int) -> date:
        return recall.today + timedelta(days=offset_days)

    def recall_item_or_404(item_id: int) -> dict:
        view = recall.item_view(item_id)
        if view is None:
            raise HTTPException(404, "리콜 항목을 찾을 수 없어요.")
        return view

    @app.get("/api/recall")
    def recall_day(offset_days: int = 0) -> dict:
        day = target_day(offset_days).isoformat()
        return {
            "date": day,
            "today": recall.today.isoformat(),
            "mode": recall.mode,
            "rules": recall.rules_view(day),
            "items": recall.items(day),
        }

    @app.post("/api/recall/plan")
    def recall_plan(body: PlanBody) -> dict:
        return recall.plan(target_day(body.offset_days), wait_for_drafts=False)

    @app.get("/api/recall/items/{item_id}")
    def recall_item(item_id: int) -> dict:
        return recall_item_or_404(item_id)

    @app.post("/api/recall/items/{item_id}/approve")
    def recall_approve(item_id: int, body: RecallApproveBody) -> dict:
        recall_item_or_404(item_id)
        try:
            recall.approve(item_id, body.text, body.handled_by)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return recall_item_or_404(item_id)

    @app.post("/api/recall/items/{item_id}/check")
    def recall_check(item_id: int, body: RecallCheckBody) -> dict:
        """고쳐 쓰는 동안의 실시간 점검. 발송 경로별 수신거부 문구·글자 수까지 승인 때와 같은 규칙으로 본다."""
        recall_item_or_404(item_id)
        return {"flags": recall.check(item_id, body.text)}

    @app.post("/api/recall/items/{item_id}/skip")
    def recall_skip(item_id: int, body: SkipBody) -> dict:
        recall_item_or_404(item_id)
        try:
            recall.skip(item_id, body.reason)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return recall_item_or_404(item_id)

    @app.post("/api/recall/items/{item_id}/cancel")
    def recall_cancel(item_id: int) -> dict:
        recall_item_or_404(item_id)
        try:
            recall.cancel(item_id)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return recall_item_or_404(item_id)

    @app.post("/api/recall/items/{item_id}/regenerate")
    def recall_regenerate(item_id: int) -> dict:
        recall_item_or_404(item_id)
        recall.regenerate(item_id)
        return recall_item_or_404(item_id)

    @app.post("/api/recall/approve-info")
    def recall_approve_info(body: PlanBody) -> dict:
        return {"approved": recall.approve_all_info(target_day(body.offset_days).isoformat())}

    app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="static")
    return app
