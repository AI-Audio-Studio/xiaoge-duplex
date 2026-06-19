import logging
import os

import httpx
import openai
from dotenv import load_dotenv

from livekit.agents import Agent, AgentServer, AgentSession, JobContext, cli
from livekit.plugins import openai as lk_openai

logger = logging.getLogger("qwen-gateway-console-agent")

load_dotenv()


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def build_llm() -> lk_openai.LLM:
    base_url = os.getenv("QWEN_BASE_URL", "https://60.205.197.165:10092/llm/v1")
    api_key = os.getenv("QWEN_API_KEY", "EMPTY")
    model = os.getenv("QWEN_MODEL", "Qwen3-4B")
    verify_ssl = _env_bool("QWEN_VERIFY_SSL", False)

    client = openai.AsyncClient(
        api_key=api_key,
        base_url=base_url,
        max_retries=0,
        http_client=httpx.AsyncClient(
            verify=verify_ssl,
            timeout=httpx.Timeout(connect=15.0, read=30.0, write=30.0, pool=30.0),
            follow_redirects=True,
            limits=httpx.Limits(
                max_connections=50,
                max_keepalive_connections=50,
                keepalive_expiry=120,
            ),
        ),
    )

    return lk_openai.LLM(
        model=model,
        client=client,
        temperature=0.7,
        top_p=0.9,
        extra_body={
            "top_k": 20,
            "max_tokens": 512,
            "presence_penalty": 1.5,
            "chat_template_kwargs": {"enable_thinking": False},
        },
    )


class QwenAgent(Agent):
    def __init__(self) -> None:
        super().__init__(
            instructions=(
                "你是一个中文助手。"
                "默认使用中文回答。"
                "回答简洁直接，先给结论，再补必要说明。"
            )
        )

    async def on_enter(self) -> None:
        self.session.generate_reply(instructions="用中文做一句简短自我介绍，并请用户直接提问。")


server = AgentServer()


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:
    ctx.log_context_fields = {
        "room_name": ctx.room.name,
        "provider": "qwen-gateway",
        "model": os.getenv("QWEN_MODEL", "Qwen3-4B"),
    }

    session = AgentSession(llm=build_llm())
    await session.start(agent=QwenAgent(), room=ctx.room)


if __name__ == "__main__":
    cli.run_app(server)
