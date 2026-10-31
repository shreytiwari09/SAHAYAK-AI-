"""Production-style entry point.

    uv run uvicorn backend.main:app --port 8000

Environment: GEMINI_API_KEY (or FAKE_LLM=1), MOCK_PAYTM_BASE_URL (or INPROCESS_MOCK=1 to run a
seeded mock Paytm inside this process), MONITOR_INTERVAL_SECONDS (0 = scan only on request).
"""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from backend.agent.runtime import build_agent, open_audit, open_checkpointer
from backend.agents import AgentRoster
from backend.api import ApiContext, create_app
from backend.channels.whatsapp import OutboxWhatsApp, WhatsAppAPI
from backend.channels.whatsapp_channel import LinkStore, WhatsAppChannel
from backend.config import get_settings
from backend.pii.masker import PiiMasker
from backend.service import CaseService
from backend.tools.client import PaytmTools


def mock_public_url(settings) -> str:
    if settings.inprocess_mock and settings.public_base_url:
        return f"{settings.public_base_url.rstrip('/')}/mock"
    return settings.mock_paytm_base_url


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    cleanup = None
    if settings.inprocess_mock:
        from backend.cli import start_inprocess_mock
        from mock_paytm.app import app as mock_app
        from mock_paytm.config import get_settings as mock_settings

        http, cleanup = await start_inprocess_mock([], "M1001", 50)
        tools = PaytmTools(http)
        app.mount("/mock", mock_app)  # so a phone can tap links through the one public address
        if settings.public_base_url:
            mock_settings().mock_paytm_public_url = f"{settings.public_base_url.rstrip('/')}/mock"
    else:
        tools = PaytmTools()

    cascade = None
    if settings.fake_llm or not settings.gemini_api_key:
        from eval.fake_llm import FakeLLM

        llm = FakeLLM()
    else:
        from backend.agent.llm import GeminiAgentLLM
        from backend.llm.cascade import GeminiCascade

        cascade = GeminiCascade()
        llm = GeminiAgentLLM(cascade)
        await cascade.warm_up()

    audit = open_audit()
    monitor_task = None
    async with open_checkpointer() as saver:
        graph, deps = build_agent(
            tools, llm, audit, saver, masker=PiiMasker(), public_url=mock_public_url(settings)
        )
        service = CaseService(graph, audit, tools, AgentRoster.load(settings.agents_file))
        await service.rehydrate()
        service.start_watcher(settings.pause_poll_seconds)

        speech = None
        if settings.sarvam_api_key:
            from backend.speech import GeminiTranscriber, SarvamSpeech

            speech = SarvamSpeech(
                settings.sarvam_api_key,
                stt_model=settings.sarvam_stt_model,
                tts_model=settings.sarvam_tts_model,
                speaker=settings.sarvam_speaker,
                cache_dir=f"{settings.data_dir}/tts_cache",
                fallback_transcriber=GeminiTranscriber(cascade) if cascade else None,
            )
        if settings.meta_wa_token and settings.meta_phone_id:
            transport = WhatsAppAPI(settings.meta_wa_token, settings.meta_phone_id)
        else:
            transport = OutboxWhatsApp()  # no credentials: record instead of sending
        channel = WhatsAppChannel(
            transport,
            service,
            LinkStore(f"{settings.data_dir}/whatsapp_links.json", settings.whatsapp_links),
            speech,
            default_merchant=settings.whatsapp_default_merchant,
            voice_replies=settings.voice_replies,
        )
        deps.on_message = channel.deliver  # every message the agent sends also reaches the phone
        app.state.ctx = ApiContext(
            service, deps.gate, audit, tools, cascade, channel,
            settings.meta_verify_token, settings.meta_app_secret, speech, settings.access_key,
        )  # fmt: skip

        if settings.monitor_interval_seconds > 0:

            async def loop():
                while True:
                    await asyncio.sleep(settings.monitor_interval_seconds)
                    await service.scan_and_start()

            monitor_task = asyncio.create_task(loop())
        yield
        if monitor_task:
            monitor_task.cancel()
        await service.aclose()
        if speech:
            await speech.aclose()
        if isinstance(transport, WhatsAppAPI):
            await transport.aclose()
    if cascade:
        await cascade.aclose()
    if cleanup:
        await cleanup()


app = create_app(lambda: app.state.ctx, lifespan=lifespan)

# The built console (cd console && npm run build) is served at /console, so one process runs it all.
_CONSOLE = Path(__file__).resolve().parent.parent / "console" / "dist"
if _CONSOLE.is_dir():
    app.mount("/console", StaticFiles(directory=_CONSOLE, html=True), name="console")
